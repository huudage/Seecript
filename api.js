/**
 * Seecript — front-end API client.
 *
 * Single Responsibility: HTTP / loading / toast only. No UI rendering, no business logic.
 *
 * Why a thin client:
 * - Backend lives at the same origin (FastAPI also serves the static frontend), so no CORS/auth.
 * - We surface user-friendly Chinese error messages that map upstream codes to plain language.
 * - Every call has a sane timeout and a single source of truth for fetch options.
 */
(function (global) {
  "use strict";

  // ---- Constants (no magic numbers) ----
  const DEFAULT_TIMEOUT_MS = 90000; // LLM round-trip can be ~30-60s; leave headroom.
  const TOAST_DURATION_MS = 3500;

  /**
   * POST a JSON body to a backend path.
   *
   * @param {string} path  - e.g. "/api/persona/generate"
   * @param {object} body  - JSON-serializable payload
   * @param {object} [opts] - { timeoutMs?: number, signal?: AbortSignal }
   * @returns {Promise<object>} parsed JSON
   */
  async function postJSON(path, body, opts) {
    const timeoutMs = (opts && opts.timeoutMs) || DEFAULT_TIMEOUT_MS;
    const ctrl = new AbortController();
    const timeoutId = setTimeout(() => ctrl.abort(), timeoutMs);

    let response;
    try {
      response = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: opts && opts.signal ? opts.signal : ctrl.signal,
        cache: "no-store",
      });
    } catch (e) {
      clearTimeout(timeoutId);
      if (e && e.name === "AbortError") {
        throw new ApiError("请求超时（>" + Math.round(timeoutMs / 1000) + "s），稍后重试。", "TIMEOUT");
      }
      throw new ApiError("网络异常：无法连接到后端。请检查 run 脚本是否已启动。", "NETWORK", e);
    }
    clearTimeout(timeoutId);

    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      throw new ApiError("后端返回了非 JSON 响应（status=" + response.status + "）。", "BAD_JSON");
    }

    if (!response.ok) {
      const detail =
        (payload && (payload.detail || payload.message)) ||
        ("HTTP " + response.status);
      const code =
        response.status === 422
          ? "VALIDATION"
          : response.status === 502
            ? "UPSTREAM"
            : "HTTP_" + response.status;
      throw new ApiError(formatHttpError(response.status, detail), code, payload);
    }

    return payload;
  }

  function formatHttpError(status, detail) {
    if (status === 422) {
      return "输入校验未通过：" + (typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    if (status === 502) {
      return "AI 服务暂时不可用：" + detail + "。可能是 API Key 未配置或上游限流，稍后重试。";
    }
    if (status >= 500) {
      return "后端内部错误（" + status + "）：" + detail;
    }
    return "请求失败（" + status + "）：" + detail;
  }

  class ApiError extends Error {
    constructor(message, code, raw) {
      super(message);
      this.name = "ApiError";
      this.code = code;
      this.raw = raw;
    }
  }

  // ---- Loading state on a button ----
  function setLoading(btn, isLoading, loadingText) {
    if (!btn) return;
    if (isLoading) {
      if (!btn.dataset.kocOriginalText) {
        btn.dataset.kocOriginalText = btn.textContent || "";
      }
      btn.disabled = true;
      btn.textContent = loadingText || "处理中…";
      btn.setAttribute("aria-busy", "true");
    } else {
      btn.disabled = false;
      if (btn.dataset.kocOriginalText) {
        btn.textContent = btn.dataset.kocOriginalText;
        delete btn.dataset.kocOriginalText;
      }
      btn.removeAttribute("aria-busy");
    }
  }

  // ---- Lightweight toast ----
  let toastEl = null;
  function ensureToastEl() {
    if (toastEl && document.body.contains(toastEl)) return toastEl;
    toastEl = document.createElement("div");
    toastEl.className = "seecript-toast";
    toastEl.setAttribute("role", "status");
    toastEl.setAttribute("aria-live", "polite");
    document.body.appendChild(toastEl);
    return toastEl;
  }

  function showToast(text, kind) {
    const el = ensureToastEl();
    el.textContent = text;
    el.dataset.kind = kind || "info"; // 'info' | 'error' | 'success'
    el.classList.add("is-visible");
    clearTimeout(el._kocTimer);
    el._kocTimer = setTimeout(() => el.classList.remove("is-visible"), TOAST_DURATION_MS);
  }

  global.SeecriptApi = {
    postJSON: postJSON,
    setLoading: setLoading,
    showToast: showToast,
    ApiError: ApiError,
  };
})(window);
