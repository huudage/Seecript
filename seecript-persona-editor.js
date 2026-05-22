/**
 * Seecript — Persona Editor (v0.10).
 *
 * 一个全局单例的 Modal 编辑器，负责把一个 persona 对象渲染为表单、收集校验后的
 * 更新并通过 onSave 回调返回。它**不直接持久化**——存写动作交给 SeecriptHistory 或
 * 调用方传进来的 onSave，符合 SRP / DIP：
 *
 *   - SRP：只管「展示 / 校验 / 提交」表单这一件事。
 *   - DIP：调用方注入 onSave 回调，本模块不依赖具体存储实现；同一个编辑器
 *          可被 feature-2.html（生成结果即时编辑）和 workspace.html
 *          （历史看板编辑）共同复用。
 *   - OCP：未来要新增字段（如 audience / pricing），改的只是 FIELD_DEFS，
 *          不需要动 open / 校验 / 提交流程。
 *
 * Public surface:
 *   window.SeecriptPersonaEditor = {
 *     open(persona, options) → Promise<boolean>  // true = 已保存
 *     close()                                     // 主动关闭（极少需要）
 *   };
 *
 * Options:
 *   - title:        modal 标题，默认 "编辑人设方案"
 *   - confirmLabel: 保存按钮文案，默认 "保存修改"
 *   - onSave(patch):  必填回调，返回 boolean 或 Promise<boolean>。
 *                    返回 false / 抛错都会让 modal 不关闭、保留用户输入；
 *                    返回 true 才认为保存成功。
 *
 * 字段：基于后端 schemas.PersonaResult（见 server/app/schemas.py）的 7 个
 * 用户可见字段——name / differentiation / rationale / onboarding_advice /
 * monetization_outlook / score / reference_accounts。
 */
(function () {
  "use strict";

  // ============================================================================
  // 字段定义（OCP：扩字段时只动这里）
  // ============================================================================
  /**
   * type:
   *   - text     → 单行 input
   *   - textarea → 多行 textarea（带 rows）
   *   - score    → 5 颗星可点（1-5）
   *   - tokens   → 多 token，逗号/顿号/空格/分号分隔，保存为 string[]
   * required:
   *   - true 时空值会拦截提交
   * maxlen:
   *   - 软上限，越界给 toast 警示但不强制截断（让用户自决）
   */
  var FIELD_DEFS = [
    {
      key: "name",
      label: "方案名 / 人设标题",
      type: "text",
      required: true,
      placeholder: "例：打工人月薪 8k 的精致冰箱整理术",
      maxlen: 40,
      hint: "30 字内最佳；这是工作台 / 第 0 步选人设时显示的主标题。",
    },
    {
      key: "differentiation",
      label: "差异化逻辑",
      type: "text",
      required: false,
      placeholder: "例：预算约束 + 长期主义",
      maxlen: 60,
      hint: "一两个关键词概括你的不可替代性，越短越好。",
    },
    {
      key: "rationale",
      label: "为何值得做",
      type: "textarea",
      rows: 3,
      required: false,
      placeholder: "例：把「收纳」×「冰箱」×「打工人预算」三层叠加，差异化空白且与生鲜/收纳品牌契合度高。",
      maxlen: 240,
    },
    {
      key: "onboarding_advice",
      label: "起号建议",
      type: "textarea",
      rows: 3,
      required: false,
      placeholder: "例：前 10 条聚焦「冰箱开箱 + 周末囤货预算」，固定每周二、五更新。",
      maxlen: 240,
    },
    {
      key: "monetization_outlook",
      label: "变现预判",
      type: "textarea",
      rows: 2,
      required: false,
      placeholder: "例：生鲜电商 / 收纳品牌植入 高；中后期可挂车。",
      maxlen: 200,
    },
    {
      key: "score",
      label: "推荐星级",
      type: "score",
      required: true,
      hint: "1-5 颗星：你对这个方案的实际信心。",
    },
    {
      key: "reference_accounts",
      label: "对标账号",
      type: "tokens",
      required: false,
      placeholder: "例：@小麦的整理日记, @省心生活, @打工人厨房",
      hint: "逗号 / 顿号 / 空格 / 分号都行；保存时自动拆成数组。",
    },
  ];

  // ============================================================================
  // DOM 单例（懒加载）
  // ============================================================================
  var modal = null;
  var titleEl = null;
  var bodyEl = null;
  var saveBtn = null;
  var cancelBtn = null;
  var closeBtn = null;
  var backdrop = null;
  var currentOptions = null;
  var keydownHandler = null;

  function ensureModal() {
    if (modal) return;
    modal = document.createElement("div");
    modal.className = "seecript-modal seecript-persona-editor-modal";
    modal.setAttribute("hidden", "");
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-modal", "true");
    modal.setAttribute("aria-labelledby", "seecript-persona-editor-title");
    modal.innerHTML =
      '<div class="seecript-modal__backdrop" data-seecript-editor-action="close"></div>' +
      '<div class="seecript-modal__panel">' +
        '<div class="seecript-modal__head">' +
          '<h3 id="seecript-persona-editor-title">编辑人设方案</h3>' +
          '<button class="seecript-modal__close" type="button" data-seecript-editor-action="close" aria-label="关闭">×</button>' +
        "</div>" +
        '<div class="seecript-modal__body seecript-persona-editor__body" data-seecript-editor-body></div>' +
        '<div class="seecript-modal__foot">' +
          '<span class="seecript-persona-editor__hint">编辑后立即覆盖本地存档，不可撤销。</span>' +
          '<div style="display:flex; gap:0.5rem;">' +
            '<button type="button" class="btn btn-ghost sm" data-seecript-editor-action="cancel">取消</button>' +
            '<button type="button" class="btn btn-primary sm" data-seecript-editor-action="save">保存修改</button>' +
          "</div>" +
        "</div>" +
      "</div>";
    document.body.appendChild(modal);

    titleEl = modal.querySelector("#seecript-persona-editor-title");
    bodyEl = modal.querySelector("[data-seecript-editor-body]");
    saveBtn = modal.querySelector('[data-seecript-editor-action="save"]');
    cancelBtn = modal.querySelector('[data-seecript-editor-action="cancel"]');
    closeBtn = modal.querySelector('.seecript-modal__close[data-seecript-editor-action="close"]');
    backdrop = modal.querySelector('.seecript-modal__backdrop[data-seecript-editor-action="close"]');

    saveBtn.addEventListener("click", handleSave);
    cancelBtn.addEventListener("click", close);
    closeBtn.addEventListener("click", close);
    backdrop.addEventListener("click", close);
  }

  // ============================================================================
  // 渲染
  // ============================================================================
  function escapeAttr(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function tokensJoin(arr) {
    if (!Array.isArray(arr)) return "";
    return arr.filter(Boolean).join("、");
  }

  function tokensSplit(text) {
    if (!text) return [];
    return String(text)
      .split(/[，,、;；\s]+/)
      .map(function (t) { return t.trim(); })
      .filter(Boolean);
  }

  function renderField(def, value) {
    var id = "seecript-pe-" + def.key;
    var hintHtml = def.hint
      ? '<div class="seecript-persona-editor__hint">' + escapeAttr(def.hint) + "</div>"
      : "";
    var headHtml =
      '<div class="seecript-persona-editor__field-head">' +
        '<label for="' + id + '">' + escapeAttr(def.label) +
        (def.required ? ' <span class="seecript-persona-editor__req">*</span>' : "") +
        "</label>" +
        (typeof def.maxlen === "number"
          ? '<span class="seecript-persona-editor__counter" data-counter-for="' + id + '">0/' + def.maxlen + "</span>"
          : "") +
      "</div>";

    if (def.type === "textarea") {
      var rows = def.rows || 3;
      return (
        '<div class="seecript-persona-editor__field" data-field-key="' + def.key + '">' +
          headHtml +
          '<textarea id="' + id + '" rows="' + rows + '" placeholder="' +
            escapeAttr(def.placeholder || "") + '">' +
            escapeAttr(value || "") +
          "</textarea>" +
          hintHtml +
        "</div>"
      );
    }
    if (def.type === "score") {
      var score = parseInt(value, 10);
      if (!Number.isFinite(score) || score < 1) score = 3;
      if (score > 5) score = 5;
      var stars = "";
      for (var s = 1; s <= 5; s++) {
        var on = s <= score;
        stars +=
          '<button type="button" class="seecript-persona-editor__star' + (on ? " is-on" : "") +
            '" data-score-value="' + s + '" aria-label="' + s + ' 星">' +
            (on ? "★" : "☆") +
          "</button>";
      }
      return (
        '<div class="seecript-persona-editor__field" data-field-key="' + def.key + '">' +
          headHtml +
          '<div class="seecript-persona-editor__stars" id="' + id + '" data-score-current="' + score + '">' +
            stars +
          "</div>" +
          hintHtml +
        "</div>"
      );
    }
    if (def.type === "tokens") {
      return (
        '<div class="seecript-persona-editor__field" data-field-key="' + def.key + '">' +
          headHtml +
          '<input type="text" id="' + id + '" placeholder="' +
            escapeAttr(def.placeholder || "") + '" value="' +
            escapeAttr(tokensJoin(value)) + '" />' +
          hintHtml +
        "</div>"
      );
    }
    return (
      '<div class="seecript-persona-editor__field" data-field-key="' + def.key + '">' +
        headHtml +
        '<input type="text" id="' + id + '" placeholder="' +
          escapeAttr(def.placeholder || "") + '" value="' +
          escapeAttr(value || "") + '" />' +
        hintHtml +
      "</div>"
    );
  }

  function bindFieldInteractivity() {
    bodyEl.querySelectorAll('input[type="text"], textarea').forEach(function (el) {
      var counter = bodyEl.querySelector('[data-counter-for="' + el.id + '"]');
      if (!counter) return;
      var def = FIELD_DEFS.find(function (d) { return "seecript-pe-" + d.key === el.id; });
      if (!def || typeof def.maxlen !== "number") return;
      function update() {
        var len = (el.value || "").length;
        counter.textContent = len + "/" + def.maxlen;
        counter.classList.toggle("is-over", len > def.maxlen);
      }
      el.addEventListener("input", update);
      update();
    });
    bodyEl.querySelectorAll(".seecript-persona-editor__stars").forEach(function (group) {
      group.querySelectorAll(".seecript-persona-editor__star").forEach(function (btn) {
        btn.addEventListener("click", function () {
          var v = parseInt(btn.dataset.scoreValue, 10);
          if (!Number.isFinite(v)) return;
          group.dataset.scoreCurrent = String(v);
          group.querySelectorAll(".seecript-persona-editor__star").forEach(function (b) {
            var bv = parseInt(b.dataset.scoreValue, 10);
            var on = bv <= v;
            b.classList.toggle("is-on", on);
            b.textContent = on ? "★" : "☆";
          });
        });
      });
    });
  }

  // ============================================================================
  // 收集 / 校验
  // ============================================================================
  function collect() {
    var patch = {};
    for (var i = 0; i < FIELD_DEFS.length; i++) {
      var def = FIELD_DEFS[i];
      var id = "seecript-pe-" + def.key;
      if (def.type === "score") {
        var group = bodyEl.querySelector('.seecript-persona-editor__stars[id="' + id + '"]');
        var cur = group ? parseInt(group.dataset.scoreCurrent, 10) : NaN;
        if (!Number.isFinite(cur)) cur = 3;
        patch.score = Math.max(1, Math.min(5, cur));
        continue;
      }
      var el = bodyEl.querySelector("#" + id);
      if (!el) continue;
      var raw = (el.value || "").trim();
      if (def.type === "tokens") {
        patch[def.key] = tokensSplit(raw);
      } else {
        patch[def.key] = raw;
      }
    }
    return patch;
  }

  function validate(patch) {
    for (var i = 0; i < FIELD_DEFS.length; i++) {
      var def = FIELD_DEFS[i];
      var v = patch[def.key];
      if (!def.required) continue;
      if (def.type === "tokens") {
        if (!Array.isArray(v) || v.length === 0) {
          return { ok: false, msg: "「" + def.label + "」必填。" };
        }
      } else if (def.type === "score") {
        if (!Number.isFinite(v) || v < 1) {
          return { ok: false, msg: "「" + def.label + "」请至少给 1 颗星。" };
        }
      } else {
        if (!v || !String(v).trim()) {
          return { ok: false, msg: "「" + def.label + "」必填。" };
        }
      }
    }
    return { ok: true };
  }

  // ============================================================================
  // 生命周期
  // ============================================================================
  function open(persona, options) {
    ensureModal();
    currentOptions = Object.assign(
      { title: "编辑人设方案", confirmLabel: "保存修改", onSave: null },
      options || {}
    );

    titleEl.textContent = currentOptions.title;
    saveBtn.textContent = currentOptions.confirmLabel;
    saveBtn.disabled = false;

    var p = persona || {};
    bodyEl.innerHTML = FIELD_DEFS.map(function (def) {
      return renderField(def, p[def.key]);
    }).join("");
    bindFieldInteractivity();

    modal.removeAttribute("hidden");
    document.body.style.overflow = "hidden";

    keydownHandler = function (e) {
      if (e.key === "Escape") close();
    };
    document.addEventListener("keydown", keydownHandler);

    var firstInput = bodyEl.querySelector('input[type="text"], textarea');
    if (firstInput) {
      try { firstInput.focus(); } catch (_) {}
    }
  }

  function close() {
    if (!modal || modal.hasAttribute("hidden")) return;
    modal.setAttribute("hidden", "");
    document.body.style.overflow = "";
    if (keydownHandler) {
      document.removeEventListener("keydown", keydownHandler);
      keydownHandler = null;
    }
    currentOptions = null;
  }

  async function handleSave() {
    if (!currentOptions || typeof currentOptions.onSave !== "function") {
      close();
      return;
    }
    var patch = collect();
    var v = validate(patch);
    if (!v.ok) {
      if (window.SeecriptApi && window.SeecriptApi.showToast) {
        SeecriptApi.showToast(v.msg, "error");
      } else {
        alert(v.msg);
      }
      return;
    }
    saveBtn.disabled = true;
    var originalLabel = saveBtn.textContent;
    saveBtn.textContent = "保存中…";
    try {
      var result = await Promise.resolve(currentOptions.onSave(patch));
      if (result === false) {
        // 调用方主动告知失败：不关闭，保留输入让用户改
        return;
      }
      close();
    } catch (e) {
      if (window.SeecriptApi && window.SeecriptApi.showToast) {
        SeecriptApi.showToast("保存失败：" + (e && e.message ? e.message : "未知错误"), "error");
      } else {
        alert("保存失败：" + (e && e.message ? e.message : "未知错误"));
      }
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = originalLabel;
    }
  }

  window.SeecriptPersonaEditor = {
    open: open,
    close: close,
  };
})();
