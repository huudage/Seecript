/**
 * Seecript — Text-to-Video page (feature-5.html) interactions.
 *
 * Single Responsibility: only feature-5 page lives here.
 *
 * Shot workflow:
 *   When sessionStorage holds a structured script (hook + scenes + cta), we render
 *   radio choices. Submitting with a selection sends `shot_preview_mode: true` so
 *   the server prepends a fixed instruction block and requests a 10s cogvideox-3 clip
 *   (preview / expectation demo). Without structured shots, the textarea is a plain
 *   prompt and `shot_preview_mode` is false.
 */
(function () {
  "use strict";

  const POLL_INTERVAL_MS = 5000;
  const POLL_HARD_TIMEOUT_MS = 8 * 60 * 1000;
  const MAX_PROMPT_CHARS = 500;
  const MAX_SHOT_BODY_CHARS = 450;
  const SS_KEY_LAST_SCRIPT = "seecript.lastScriptForT2V";
  const ELAPSED_TICK_MS = 1000;
  const VISUAL_HINT_TAKE = 2;

  const state = {
    taskId: null,
    pollAbort: null,
    elapsedTimerId: null,
    startedAt: 0,
    shots: [],
    selectedShotIndex: -1,
  };

  function $(sel) {
    return document.querySelector(sel);
  }

  function showStage(name) {
    document.querySelectorAll("[data-seecript-stage]").forEach((el) => {
      el.hidden = el.dataset.kocStage !== name;
    });
  }

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  function buildPromptFromScript(scriptObj) {
    const out = { text: "", truncated: false };
    if (!scriptObj || typeof scriptObj !== "object") return out;

    const full = typeof scriptObj.full_text === "string" ? scriptObj.full_text.trim() : "";
    if (full) {
      out.truncated = full.length > MAX_PROMPT_CHARS;
      out.text = full.slice(0, MAX_PROMPT_CHARS);
      return out;
    }

    const scenes = Array.isArray(scriptObj.scenes) ? scriptObj.scenes : [];
    const visuals = scenes
      .map((s) => (s && typeof s.visual === "string") ? s.visual.trim() : "")
      .filter(Boolean)
      .slice(0, VISUAL_HINT_TAKE);
    if (visuals.length) {
      const joined = visuals.join("，");
      out.truncated = joined.length > MAX_PROMPT_CHARS;
      out.text = joined.slice(0, MAX_PROMPT_CHARS);
      return out;
    }
    const hook = typeof scriptObj.hook_narration === "string" ? scriptObj.hook_narration.trim() : "";
    out.truncated = hook.length > MAX_PROMPT_CHARS;
    out.text = hook.slice(0, MAX_PROMPT_CHARS);
    return out;
  }

  function readScriptFromSession() {
    try {
      const raw = sessionStorage.getItem(SS_KEY_LAST_SCRIPT);
      if (!raw) return null;
      return JSON.parse(raw);
    } catch (_) {
      return null;
    }
  }

  /**
   * Build selectable shots; `body` is the user/shot slice sent as `prompt` when
   * `shot_preview_mode` is true (server merges system prefix + caps at 500 chars).
   */
  function buildShots(scriptObj) {
    const rows = [];
    if (!scriptObj || typeof scriptObj !== "object") return rows;

    function pushRow(id, label, parts) {
      const body = parts
        .filter(Boolean)
        .map((p) => String(p).trim())
        .filter(Boolean)
        .join("；");
      if (!body) return;
      rows.push({ id, label, body: body.slice(0, MAX_SHOT_BODY_CHARS) });
    }

    const hook = typeof scriptObj.hook_narration === "string" ? scriptObj.hook_narration.trim() : "";
    const scenes = Array.isArray(scriptObj.scenes) ? scriptObj.scenes : [];
    const firstVisual =
      scenes[0] && typeof scenes[0].visual === "string" ? scenes[0].visual.trim() : "";
    if (hook) {
      pushRow("hook", "Hook · 开场（约前 3 秒）", [firstVisual, hook]);
    }

    scenes.forEach((s, i) => {
      if (!s || typeof s !== "object") return;
      const ts = (s.timestamp || "").trim();
      const title = (s.title || "分镜 " + (i + 1)).trim();
      const lab = (ts ? ts + " · " : "") + title;
      pushRow("scene-" + i, "正文 · " + lab, [s.visual, s.narration]);
    });

    const cta = typeof scriptObj.cta_narration === "string" ? scriptObj.cta_narration.trim() : "";
    if (cta) {
      pushRow("cta", "CTA · 收尾", [cta]);
    }
    return rows;
  }

  function applyShotSelection(idx, silent) {
    state.selectedShotIndex = idx;
    const ta = $("#t2v-prompt");
    const row = state.shots[idx];
    if (!ta || !row) return;
    ta.value = row.body;
    ta.dispatchEvent(new Event("input", { bubbles: true }));
    if (!silent) {
      SeecriptApi.showToast("已切换至：「" + row.label + "」", "info");
    }
  }

  function renderShotRadios() {
    const panel = document.getElementById("t2v-segment-panel");
    const host = document.getElementById("t2v-shot-radios");
    const hint = document.getElementById("t2v-shot-mode-hint");
    if (!panel || !host) return;

    const obj = readScriptFromSession();
    state.shots = buildShots(obj);
    state.selectedShotIndex = state.shots.length ? 0 : -1;

    if (!state.shots.length) {
      panel.hidden = true;
      host.innerHTML = "";
      if (hint) hint.hidden = true;
      return;
    }

    panel.hidden = false;
    if (hint) hint.hidden = false;
    host.innerHTML = "";

    state.shots.forEach((row, idx) => {
      const wrap = document.createElement("div");
      wrap.className = "t2v-shot-row";
      const inp = document.createElement("input");
      inp.type = "radio";
      inp.name = "t2v-shot";
      inp.value = String(idx);
      inp.id = "t2v-shot-choice-" + idx;
      if (idx === 0) inp.checked = true;

      inp.addEventListener("change", () => {
        if (inp.checked) applyShotSelection(idx, false);
      });

      const lab = document.createElement("label");
      lab.htmlFor = inp.id;
      const strong = document.createElement("strong");
      strong.textContent = row.label;
      lab.appendChild(strong);
      const meta = document.createElement("span");
      meta.className = "t2v-shot-meta";
      const preview = row.body.length > 140 ? row.body.slice(0, 140) + "…" : row.body;
      meta.textContent = preview;
      lab.appendChild(meta);

      wrap.appendChild(inp);
      wrap.appendChild(lab);
      wrap.addEventListener("click", (ev) => {
        if (ev.target === inp) return;
        inp.checked = true;
        inp.dispatchEvent(new Event("change", { bubbles: true }));
      });
      host.appendChild(wrap);
    });
  }

  function bindPromptForm() {
    const ta = $("#t2v-prompt");
    const counter = document.querySelector("[data-seecript-prompt-counter]");
    const importBtn = document.querySelector('[data-seecript-action="import-script"]');
    if (!ta) return;

    function updateCount() {
      const len = ta.value.length;
      if (!counter) return;
      counter.textContent = len + " / " + MAX_PROMPT_CHARS;
      counter.style.color = len > MAX_PROMPT_CHARS ? "var(--danger, #c0392b)" : "";
    }
    ta.addEventListener("input", updateCount);

    renderShotRadios();

    if (state.shots.length) {
      applyShotSelection(0, true);
    } else if (!ta.value) {
      const obj = readScriptFromSession();
      if (obj) {
        const result = buildPromptFromScript(obj);
        ta.value = result.text;
        if (result.truncated) {
          SeecriptApi.showToast(
            "原创脚本超过 " + MAX_PROMPT_CHARS + " 字，已截取前 " + MAX_PROMPT_CHARS + " 字作为素材描述（提示词）。",
            "info"
          );
        }
      }
    }

    if (importBtn) {
      importBtn.addEventListener("click", () => {
        const obj = readScriptFromSession();
        if (!obj) {
          SeecriptApi.showToast(
            "没有可带入的脚本——请先在「爆款拆解」完成第 4 步生成原创脚本。",
            "error"
          );
          return;
        }
        renderShotRadios();
        if (state.shots.length) {
          applyShotSelection(0, true);
          SeecriptApi.showToast("已载入脚本分镜列表，请选择一个分镜后生成演示。", "success");
        } else {
          const result = buildPromptFromScript(obj);
          ta.value = result.text;
          if (result.truncated) {
            SeecriptApi.showToast(
              "原创脚本超过 " + MAX_PROMPT_CHARS + " 字，已截取前 " + MAX_PROMPT_CHARS + " 字作为素材描述（提示词）。",
              "info"
            );
          } else {
            SeecriptApi.showToast("已载入原创脚本全文作为提示词。", "success");
          }
        }
        updateCount();
      });
    }

    updateCount();
  }

  async function startGenerate(submitBtn) {
    const ta = $("#t2v-prompt");
    const sizeSel = $("#t2v-size");
    const qualitySel = $("#t2v-quality");
    const audioCb = $("#t2v-with-audio");

    const prompt = (ta && ta.value || "").trim();
    if (!prompt) {
      SeecriptApi.showToast("请填写素材描述（提示词），或先选择分镜。", "error");
      if (ta) ta.focus();
      return;
    }
    if (prompt.length < 4) {
      SeecriptApi.showToast("提示词太短（建议 ≥ 20 字）。", "error");
      return;
    }
    if (prompt.length > MAX_PROMPT_CHARS) {
      SeecriptApi.showToast(
        "提示词过长（" + prompt.length + " 字 > 上限 " + MAX_PROMPT_CHARS + " 字）",
        "error"
      );
      return;
    }

    const shotPreview = state.shots.length > 0 && state.selectedShotIndex >= 0;

    showStage("loading");
    state.startedAt = Date.now();
    startElapsedTicker();
    SeecriptApi.setLoading(submitBtn, true, "提交中…");

    let submitResp;
    try {
      submitResp = await SeecriptApi.postJSON("/api/t2v/submit", {
        prompt: prompt,
        size: sizeSel ? sizeSel.value : "720x1280",
        quality: qualitySel ? qualitySel.value : "speed",
        with_audio: audioCb ? audioCb.checked : false,
        shot_preview_mode: shotPreview,
      });
    } catch (e) {
      stopElapsedTicker();
      showError(e.message || "提交失败，请重试。");
      SeecriptApi.setLoading(submitBtn, false);
      return;
    }
    SeecriptApi.setLoading(submitBtn, false);

    state.taskId = submitResp.task_id;
    const idEl = document.querySelector("[data-seecript-task-id]");
    const providerEl = document.querySelector("[data-seecript-provider]");
    if (idEl) idEl.textContent = submitResp.task_id;
    if (providerEl) providerEl.textContent = submitResp.provider;
    SeecriptApi.showToast("任务已提交（" + submitResp.provider + "），正在生成…", "info");

    pollUntilDone();
  }

  async function pollUntilDone() {
    const taskId = state.taskId;
    if (!taskId) return;
    const path = "/api/t2v/query/" + encodeURIComponent(taskId);
    const deadline = Date.now() + POLL_HARD_TIMEOUT_MS;

    while (Date.now() < deadline) {
      if (state.taskId !== taskId) return;

      let resp;
      try {
        const r = await fetch(path, { cache: "no-store" });
        resp = await r.json();
        if (!r.ok) {
          throw new Error(resp && (resp.detail || resp.message) || ("HTTP " + r.status));
        }
      } catch (e) {
        state._consecutiveErrors = (state._consecutiveErrors || 0) + 1;
        if (state._consecutiveErrors >= 3) {
          stopElapsedTicker();
          showError("查询多次失败：" + (e.message || "网络异常") + "。请稍后重试。");
          return;
        }
        await sleep(POLL_INTERVAL_MS);
        continue;
      }
      state._consecutiveErrors = 0;

      if (resp.status === "succeeded") {
        stopElapsedTicker();
        renderResult(resp);
        return;
      }
      if (resp.status === "failed") {
        stopElapsedTicker();
        showError(resp.fail_reason || "上游模型返回失败状态，未提供具体原因。");
        return;
      }
      await sleep(POLL_INTERVAL_MS);
    }

    stopElapsedTicker();
    showError(
      "已等待 8 分钟仍未完成——任务可能在排队。task_id：" +
        taskId +
        "（你可以稍后用「查询任务」按钮重试或刷新本页）。"
    );
  }

  function renderResult(resp) {
    showStage("result");
    const video = $("#t2v-result-video");
    const dl = document.querySelector('[data-seecript-action="download-video"]');
    const taskIdEl = document.querySelector("[data-seecript-result-task-id]");
    const promptEl = document.querySelector("[data-seecript-result-prompt]");
    const usedSeconds = Math.round((Date.now() - state.startedAt) / 1000);
    const durEl = document.querySelector("[data-seecript-result-duration]");

    if (video && resp.video_url) {
      video.src = resp.video_url;
      if (resp.cover_image_url) video.poster = resp.cover_image_url;
    }
    if (dl && resp.video_url) {
      dl.href = resp.video_url;
      dl.download = "seecript-" + (resp.task_id || "video") + ".mp4";
      dl.style.display = "inline-block";
    }
    if (taskIdEl) taskIdEl.textContent = resp.task_id || "-";
    if (promptEl) {
      const promptVal = ($("#t2v-prompt") && $("#t2v-prompt").value) || "";
      promptEl.textContent = promptVal;
    }
    if (durEl) durEl.textContent = usedSeconds + " 秒";

    SeecriptApi.showToast("分镜素材生成完成 · 用时 " + usedSeconds + " 秒", "success");
  }

  function showError(msg) {
    showStage("error");
    const errEl = document.querySelector("[data-seecript-error]");
    if (errEl) errEl.textContent = msg;
    SeecriptApi.showToast(msg.length > 80 ? msg.slice(0, 80) + "…" : msg, "error");
  }

  function startElapsedTicker() {
    state.elapsedTimerId = setInterval(() => {
      const el = document.querySelector("[data-seecript-elapsed]");
      if (!el) return;
      el.textContent = Math.round((Date.now() - state.startedAt) / 1000) + "s";
    }, ELAPSED_TICK_MS);
  }

  function stopElapsedTicker() {
    if (state.elapsedTimerId) {
      clearInterval(state.elapsedTimerId);
      state.elapsedTimerId = null;
    }
  }

  function bindGenerate() {
    const btn = document.querySelector('[data-seecript-action="start-generate"]');
    if (!btn) return;
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      startGenerate(btn);
    });
  }

  function bindRegenerate() {
    document.querySelectorAll('[data-seecript-action="regenerate"]').forEach((btn) => {
      btn.addEventListener("click", () => {
        state.taskId = null;
        state._consecutiveErrors = 0;
        stopElapsedTicker();
        showStage("input");
      });
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindPromptForm();
    bindGenerate();
    bindRegenerate();
    showStage("input");
  });
})();
