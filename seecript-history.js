/**
 * Seecript — local history store.
 *
 * Why this file exists:
 *   The product needs a "我的人设方案" / "我的脚本项目" board on the workspace,
 *   plus the ability to expand each entry to see the full content, copy it, or
 *   bring it back into a downstream step (标题车间). We keep the storage 100 %
 *   client-side (localStorage) for v1 — there is no auth and the personas are
 *   not sensitive enough to warrant a server table. If we ever add multi-device
 *   sync, this file becomes the single point that talks to a future backend;
 *   the rest of the app keeps calling SeecriptHistory.* unchanged.
 *
 * Public surface (window.SeecriptHistory):
 *   - savePersonas(personas, inputs)       → 人设生成成功后立即保存（一步操作）
 *   - saveScript({ persona, transcript, skeleton, answers, script })
 *                                          → 仅在脚本生成成功（feature-1 第 4 步）后调用，
 *                                            把「人设 / 台词 / 骨架 / 3 答案 / 脚本」打成
 *                                            一个完整可回放记录。中途放弃不会污染历史。
 *   - listPersonas() / listScripts()       → 倒序，最多 MAX_ITEMS 条
 *   - getPersona(recordId, personaIdx)     → 单方案查询（v0.10 起；编辑器初始化用）
 *   - updatePersona(recordId, personaIdx, updates)
 *                                          → v0.10 起：原地修改某条 personas[idx] 的部分字段
 *                                            （并把同 record 在 SeecriptActivePersona 中的快照同步刷新）。
 *                                            返回更新后的 persona 对象；找不到时返回 null。
 *   - removePersona(id) / removeScript(id)
 *   - renderBoards()                       → 工作台调用，幂等
 *   - getKpis()                            → { personasTotal, scriptsTotal, scriptsThisWeek, hoursSaved }
 *
 * Storage schema (LS keys):
 *   seecript.personas.v1   = [{ id, createdAt, inputs:{background,interests,resources}, personas:[...] }]
 *   seecript.scripts.v1    = [{
 *     id, createdAt,
 *     persona:    { name, differentiation, rationale, score },  // 第 0 步选中的方案（冗余存）
 *     transcript: "原视频台词全文",
 *     skeleton:   { hook, body, cta, transferable_template },
 *     answers:    [{ round, question, choice }, ...],          // ≤ 3 条
 *     script:     { hook_narration, scenes:[], cta_narration, full_text },
 *   }]
 *   seecript.skeletons.v1  → 旧版 schema（v0.6 之前），新代码不再写入；读侧也不展示，避免与
 *                       新「脚本项目」概念混淆。需要历史数据时手动 localStorage.getItem 取。
 *
 * Versioned key suffix (.v1) so we can migrate without nuking user data.
 */
(function () {
  "use strict";

  // ---- constants ----------------------------------------------------------
  // Cap to keep localStorage well under the ~5 MB quota even with long bodies.
  var MAX_ITEMS = 30;
  var WEEK_MS = 7 * 24 * 60 * 60 * 1000;
  // 1 个完整脚本项目 = 拆解 + 3 轮问答 + 脚本生成 ≈ 节省 1.5 小时人工创作。
  // 比之前按"拆解一次=2h"更接近真实贡献（早期统计偏乐观）。
  var HOURS_PER_SCRIPT = 1.5;

  var LS_PERSONAS = "seecript.personas.v1";
  var LS_SCRIPTS = "seecript.scripts.v1";

  // ---- low-level storage --------------------------------------------------
  function readArray(key) {
    try {
      var raw = window.localStorage.getItem(key);
      if (!raw) return [];
      var parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (_e) {
      // Corrupt JSON → fail-safe to empty list, never throw on UI hot path.
      return [];
    }
  }

  function writeArray(key, list) {
    try {
      window.localStorage.setItem(key, JSON.stringify(list.slice(0, MAX_ITEMS)));
    } catch (_e) {
      // Quota exceeded or storage disabled — silently drop the write.
      // (Boards just won't update; no app-breaking error.)
    }
  }

  function newId() {
    // Short non-cryptographic id is fine — uniqueness within one user's browser only.
    return (
      Date.now().toString(36) + "-" + Math.floor(Math.random() * 0xffffffff).toString(36)
    );
  }

  // ---- save APIs ----------------------------------------------------------
  function savePersonas(personas, inputs) {
    if (!Array.isArray(personas) || personas.length === 0) return null;
    var record = {
      id: newId(),
      createdAt: Date.now(),
      inputs: inputs || {},
      personas: personas,
    };
    var list = [record].concat(readArray(LS_PERSONAS));
    writeArray(LS_PERSONAS, list);
    return record;
  }

  /**
   * 保存一个完整的脚本项目（仅在 feature-1 第 4 步出脚本之后调用）。
   *
   * @param {Object} payload
   * @param {Object} [payload.persona]    第 0 步选定的人设 { name, differentiation, rationale, score }
   * @param {string} [payload.transcript] 第 1 步原视频台词
   * @param {Object} payload.skeleton     第 2 步骨架
   * @param {Array}  [payload.answers]    第 3 步 3 轮答案
   * @param {Object} payload.script       第 4 步脚本 { hook_narration, scenes, cta_narration, full_text }
   * @returns {Object|null} 保存的 record（含 id），失败返回 null
   */
  function saveScript(payload) {
    if (!payload || !payload.script || !payload.script.full_text) return null;
    var record = {
      id: newId(),
      createdAt: Date.now(),
      persona: payload.persona || null,
      transcript: payload.transcript || "",
      skeleton: payload.skeleton || null,
      answers: payload.answers || [],
      script: payload.script,
    };
    var list = [record].concat(readArray(LS_SCRIPTS));
    writeArray(LS_SCRIPTS, list);
    return record;
  }

  function listPersonas() { return readArray(LS_PERSONAS); }
  function listScripts() { return readArray(LS_SCRIPTS); }

  /**
   * 单方案查询。编辑器打开时调用以拿到完整字段（包括 reference_accounts 数组）。
   * @returns {Object|null} 命中则返回 personas[personaIdx]，否则 null。
   */
  function getPersona(recordId, personaIdx) {
    if (!recordId) return null;
    var idx = parseInt(personaIdx, 10);
    if (!Number.isFinite(idx) || idx < 0) return null;
    var rec = listPersonas().find(function (r) { return r.id === recordId; });
    if (!rec || !Array.isArray(rec.personas) || !rec.personas[idx]) return null;
    return rec.personas[idx];
  }

  /**
   * 原地修改一个已保存的 persona。
   *
   * 设计要点（SOLID/SRP）：
   *   - 这是 SeecriptHistory 模块的唯一写入路径，外部模块不应直接 setItem(LS_PERSONAS,...)；
   *     UI（feature-2 卡片 / workspace 看板 / seecript-persona-editor）一律通过本函数。
   *   - 字段白名单合并而非 Object.assign(record, updates)，是为了拒绝调用方把
   *     id / createdAt 等元数据改坏（防御性编程）。
   *   - 当被改的方案恰好是 SeecriptActivePersona 当前选中的那一个时，同步刷新
   *     sessionStorage 里的快照——否则用户修改后跳到爆款拆解页发现"显示的还是旧名字"。
   *
   * @param {string} recordId
   * @param {number} personaIdx
   * @param {Object} updates  允许字段：name / differentiation / rationale /
   *                          onboarding_advice / monetization_outlook / score /
   *                          reference_accounts (string[])
   * @returns {Object|null}   更新后的 persona 对象；记录不存在或 idx 越界时返回 null
   */
  function updatePersona(recordId, personaIdx, updates) {
    if (!recordId || !updates || typeof updates !== "object") return null;
    var idx = parseInt(personaIdx, 10);
    if (!Number.isFinite(idx) || idx < 0) return null;

    var list = readArray(LS_PERSONAS);
    var pos = list.findIndex(function (r) { return r.id === recordId; });
    if (pos < 0) return null;
    var rec = list[pos];
    if (!Array.isArray(rec.personas) || !rec.personas[idx]) return null;

    var allowed = [
      "name",
      "differentiation",
      "rationale",
      "onboarding_advice",
      "monetization_outlook",
      "score",
      "reference_accounts",
    ];
    var current = rec.personas[idx];
    var merged = Object.assign({}, current);
    allowed.forEach(function (k) {
      if (Object.prototype.hasOwnProperty.call(updates, k)) {
        merged[k] = updates[k];
      }
    });

    if (typeof merged.score !== "undefined") {
      var s = parseInt(merged.score, 10);
      merged.score = Number.isFinite(s) ? Math.max(1, Math.min(5, s)) : current.score;
    }
    if (typeof merged.reference_accounts !== "undefined" && !Array.isArray(merged.reference_accounts)) {
      merged.reference_accounts = current.reference_accounts || [];
    }

    rec.personas[idx] = merged;
    list[pos] = rec;
    writeArray(LS_PERSONAS, list);

    syncActivePersonaSnapshot(recordId, idx, merged, rec.inputs);
    return merged;
  }

  /**
   * 当被编辑的人设恰好是 feature-1 第 0 步选中的那一个时，把
   * sessionStorage["seecript.activePersona"] 同步刷新。
   *
   * 这里直接对 sessionStorage 操作而非通过 SeecriptActivePersona.save()——因为
   * seecript-history.js 早于 interactions.js 加载，且 seecript-history.js 也跑在
   * workspace.html 这种没有 SeecriptActivePersona 实例的页面上；任何地方都用
   * 一套幂等的 sessionStorage IO 是最安全的耦合方式。
   */
  function syncActivePersonaSnapshot(recordId, personaIdx, persona, recordInputs) {
    try {
      var raw = window.sessionStorage.getItem("seecript.activePersona");
      if (!raw) return;
      var snap = JSON.parse(raw);
      if (!snap || snap.recordId !== recordId || snap.personaIdx !== personaIdx) return;
      snap.name = persona.name || "";
      snap.differentiation = persona.differentiation || "";
      snap.rationale = persona.rationale || "";
      snap.score = persona.score || 0;
      snap.inputs = recordInputs || snap.inputs || {};
      window.sessionStorage.setItem("seecript.activePersona", JSON.stringify(snap));
    } catch (_e) {
      // 隐私模式下 sessionStorage 不可用——跳过同步即可，不影响 LS 已经写成功的事实。
    }
  }

  function removeBy(key, id) {
    var list = readArray(key).filter(function (item) { return item.id !== id; });
    writeArray(key, list);
  }
  function removePersona(id) { removeBy(LS_PERSONAS, id); }
  function removeScript(id) { removeBy(LS_SCRIPTS, id); }

  // ---- KPIs ---------------------------------------------------------------
  function getKpis() {
    var personas = listPersonas();
    var scripts = listScripts();
    var weekAgo = Date.now() - WEEK_MS;
    var scriptsThisWeek = scripts.filter(function (s) {
      return s.createdAt >= weekAgo;
    }).length;
    return {
      personasTotal: personas.reduce(function (acc, r) {
        return acc + (Array.isArray(r.personas) ? r.personas.length : 0);
      }, 0),
      scriptsTotal: scripts.length,
      scriptsThisWeek: scriptsThisWeek,
      hoursSaved: Math.round(scripts.length * HOURS_PER_SCRIPT * 10) / 10,
    };
  }

  // ---- formatting helpers -------------------------------------------------
  function escapeHtml(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmtTime(ts) {
    var d = new Date(ts);
    var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
    return (
      d.getMonth() + 1 + "/" + d.getDate() + " " + pad(d.getHours()) + ":" + pad(d.getMinutes())
    );
  }

  function fmtScore(n) {
    var stars = Math.max(0, Math.min(5, parseInt(n, 10) || 0));
    return "★".repeat(stars) + "☆".repeat(5 - stars);
  }

  // ---- rendering: Personas ------------------------------------------------
  // 渲染思路：每条 personas 记录默认收起一行摘要（标题/输入/数量+时间），
  // 点「展开 ↓」内联展示该记录里全部 personas[] 方案 + 起号建议。
  function renderPersonasBoard(ul) {
    var list = listPersonas();
    if (list.length === 0) return; // 保留 HTML 自带的 empty-state 文案
    ul.innerHTML = list
      .slice(0, 6)
      .map(function (rec) {
        var top = (rec.personas && rec.personas[0]) || {};
        var title = escapeHtml(top.name || "未命名人设");
        var meta = escapeHtml(
          [rec.inputs && rec.inputs.background, rec.inputs && rec.inputs.interests]
            .filter(Boolean)
            .join(" · ")
        );
        var count = rec.personas ? rec.personas.length : 0;
        var pillTxt = "共 " + count + " 个 · " + fmtTime(rec.createdAt);

        var detailsHtml = (rec.personas || [])
          .map(function (p, idx) {
            // 「编辑此方案」按钮放在 head 的右上角，与 score 同行——保持视觉
            // 对称的同时降低误点删除的概率（删除按钮在 seecript-history-item__row）。
            return (
              '<div class="seecript-history-detail__item">' +
                '<div class="seecript-history-detail__head">' +
                  "<b>方案 " + (idx + 1) + " · " + escapeHtml(p.name || "未命名") + "</b>" +
                  '<span class="seecript-history-detail__head-right">' +
                    '<span class="seecript-history-detail__score">' + fmtScore(p.score) + "</span>" +
                    '<button type="button" class="btn btn-ghost xs seecript-history-detail__edit"' +
                      ' data-action="edit-persona"' +
                      ' data-record-id="' + escapeHtml(rec.id) + '"' +
                      ' data-persona-idx="' + idx + '"' +
                      ' aria-label="编辑此方案">' +
                      '✎ 编辑' +
                    "</button>" +
                  "</span>" +
                "</div>" +
                (p.differentiation ? "<p>差异化 · " + escapeHtml(p.differentiation) + "</p>" : "") +
                (p.rationale ? "<p>为何值得做 · " + escapeHtml(p.rationale) + "</p>" : "") +
                (p.onboarding_advice ? "<p>起号建议 · " + escapeHtml(p.onboarding_advice) + "</p>" : "") +
                (p.monetization_outlook ? "<p>变现预判 · " + escapeHtml(p.monetization_outlook) + "</p>" : "") +
                (Array.isArray(p.reference_accounts) && p.reference_accounts.length
                  ? "<p>对标账号 · " + escapeHtml(p.reference_accounts.join("、")) + "</p>"
                  : "") +
              "</div>"
            );
          })
          .join("");

        return (
          '<li class="seecript-history-item" data-id="' + escapeHtml(rec.id) + '">' +
            '<div class="seecript-history-item__row">' +
              "<b>" + title + "</b>" +
              "<span>" + (meta || "—") + "</span>" +
              '<span class="seecript-pill">' + escapeHtml(pillTxt) + "</span>" +
              '<button type="button" class="btn btn-ghost sm seecript-history-toggle" data-target="persona-detail-' + escapeHtml(rec.id) + '">展开 ↓</button>' +
              ' <button type="button" class="seecript-history-del" data-kind="persona" data-id="' + escapeHtml(rec.id) + '" aria-label="删除">×</button>' +
            "</div>" +
            '<div class="seecript-history-detail" id="persona-detail-' + escapeHtml(rec.id) + '" hidden>' + detailsHtml + "</div>" +
          "</li>"
        );
      })
      .join("");
  }

  // ---- rendering: Scripts -------------------------------------------------
  // 渲染思路：每条 script 项目默认收起，展开后展示：
  //   - 选用人设（第 0 步）
  //   - 原台词（第 1 步，最多 200 字预览）
  //   - 骨架（第 2 步，hook + body 段数 + cta）
  //   - 3 答案（第 3 步）
  //   - 完整脚本（第 4 步，可滚动）
  // 同时给 [复制脚本] [带入标题车间] [删除] 三个动作按钮。
  function renderScriptsBoard(ul) {
    var list = listScripts();
    if (list.length === 0) return;
    ul.innerHTML = list
      .slice(0, 6)
      .map(function (rec) {
        var hookText =
          (rec.script && rec.script.hook_narration) ||
          (rec.skeleton && rec.skeleton.hook && rec.skeleton.hook.text) ||
          "—";
        var transcriptPreview = rec.transcript ? rec.transcript.slice(0, 28) + (rec.transcript.length > 28 ? "…" : "") : "";
        var personaName = (rec.persona && rec.persona.name) || "未指定人设";
        var sceneCount = (rec.script && rec.script.scenes) ? rec.script.scenes.length : 0;
        var fullChars = (rec.script && rec.script.full_text) ? rec.script.full_text.length : 0;
        var pillTxt = sceneCount + " 段 · " + fullChars + " 字 · " + fmtTime(rec.createdAt);

        var answersHtml = (rec.answers || [])
          .map(function (a) {
            return (
              '<div class="seecript-history-detail__answer">' +
                "<b>第 " + a.round + " 题</b> · " + escapeHtml(a.question || "") +
                '<br/><span class="seecript-history-detail__choice">✓ ' + escapeHtml(a.choice || "") + "</span>" +
              "</div>"
            );
          })
          .join("");

        var skeletonHtml = "";
        if (rec.skeleton) {
          var hook = rec.skeleton.hook || {};
          var cta = rec.skeleton.cta || {};
          skeletonHtml =
            '<div class="seecript-history-detail__sub">' +
              "<b>骨架 · Hook</b> · " + escapeHtml(hook.text || "—") +
              "<br/><b>骨架 · Body</b> · " + ((rec.skeleton.body || []).length) + " 段" +
              "<br/><b>骨架 · CTA</b> · " + escapeHtml(cta.text || "—") +
            "</div>";
        }

        var fullScriptHtml = (rec.script && rec.script.full_text)
          ? '<div class="seecript-history-detail__script"><pre>' + escapeHtml(rec.script.full_text) + "</pre></div>"
          : "";

        return (
          '<li class="seecript-history-item" data-id="' + escapeHtml(rec.id) + '">' +
            '<div class="seecript-history-item__row">' +
              "<b>" + escapeHtml(personaName) + (transcriptPreview ? " · " + escapeHtml(transcriptPreview) : "") + "</b>" +
              "<span>" + escapeHtml(hookText.slice(0, 60)) + "</span>" +
              '<span class="seecript-pill">' + escapeHtml(pillTxt) + "</span>" +
              '<button type="button" class="btn btn-ghost sm seecript-history-toggle" data-target="script-detail-' + escapeHtml(rec.id) + '">展开 ↓</button>' +
              ' <button type="button" class="seecript-history-del" data-kind="script" data-id="' + escapeHtml(rec.id) + '" aria-label="删除">×</button>' +
            "</div>" +
            '<div class="seecript-history-detail" id="script-detail-' + escapeHtml(rec.id) + '" hidden>' +
              (rec.persona
                ? '<div class="seecript-history-detail__sub"><b>选用人设 · </b>' +
                    escapeHtml(rec.persona.name || "—") +
                    (rec.persona.differentiation ? "（" + escapeHtml(rec.persona.differentiation) + "）" : "") +
                  "</div>"
                : "") +
              skeletonHtml +
              (answersHtml ? '<div class="seecript-history-detail__sub"><b>3 个创作决策</b></div>' + answersHtml : "") +
              fullScriptHtml +
              '<div class="seecript-history-detail__actions">' +
                '<button type="button" class="btn btn-primary sm" data-action="copy-script" data-id="' + escapeHtml(rec.id) + '">复制脚本到剪贴板</button>' +
                '<button type="button" class="btn btn-ghost sm" data-action="bring-to-seo" data-id="' + escapeHtml(rec.id) + '">带入标题车间 →</button>' +
              "</div>" +
            "</div>" +
          "</li>"
        );
      })
      .join("");
  }

  // ---- bindings -----------------------------------------------------------
  function bindToggleButtons() {
    document.querySelectorAll(".seecript-history-toggle").forEach(function (btn) {
      if (btn.dataset.kocBound === "1") return;
      btn.dataset.kocBound = "1";
      btn.addEventListener("click", function () {
        var target = document.getElementById(btn.dataset.target);
        if (!target) return;
        var willOpen = target.hasAttribute("hidden");
        if (willOpen) {
          target.removeAttribute("hidden");
          btn.textContent = "收起 ↑";
        } else {
          target.setAttribute("hidden", "");
          btn.textContent = "展开 ↓";
        }
      });
    });
  }

  function bindDeleteButtons() {
    document.querySelectorAll(".seecript-history-del").forEach(function (btn) {
      if (btn.dataset.kocBound === "1") return;
      btn.dataset.kocBound = "1";
      btn.addEventListener("click", function () {
        var kind = btn.dataset.kind;
        var id = btn.dataset.id;
        if (!confirm("确认删除这条本地记录吗？此操作不可撤销。")) return;
        if (kind === "persona") removePersona(id);
        else if (kind === "script") removeScript(id);
        renderBoards();
      });
    });
  }

  // 「复制脚本到剪贴板」与「带入标题车间」是 script 项目专属。
  // 后者通过 sessionStorage["seecript.lastScriptForSeo"] 把脚本传递到 feature-3，
  // 与「feature-1 第 4 步生成完自动写入」是同一个 key，复用同一个消费方。
  function bindScriptActions() {
    document.querySelectorAll('[data-action="copy-script"]').forEach(function (btn) {
      if (btn.dataset.kocBound === "1") return;
      btn.dataset.kocBound = "1";
      btn.addEventListener("click", async function () {
        var id = btn.dataset.id;
        var rec = listScripts().find(function (r) { return r.id === id; });
        if (!rec || !rec.script || !rec.script.full_text) {
          alert("该项目缺少脚本文本，无法复制。");
          return;
        }
        var text = rec.script.full_text;
        try {
          if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
          } else {
            var ta = document.createElement("textarea");
            ta.value = text;
            ta.style.position = "fixed";
            ta.style.opacity = "0";
            document.body.appendChild(ta);
            ta.select();
            document.execCommand("copy");
            document.body.removeChild(ta);
          }
          if (window.SeecriptApi && window.SeecriptApi.showToast) {
            SeecriptApi.showToast("已复制脚本（" + text.length + " 字）", "success");
          }
        } catch (e) {
          alert("复制失败：" + (e.message || e));
        }
      });
    });
    document.querySelectorAll('[data-action="bring-to-seo"]').forEach(function (btn) {
      if (btn.dataset.kocBound === "1") return;
      btn.dataset.kocBound = "1";
      btn.addEventListener("click", function () {
        var id = btn.dataset.id;
        var rec = listScripts().find(function (r) { return r.id === id; });
        if (!rec || !rec.script || !rec.script.full_text) {
          alert("该项目缺少脚本文本，无法带入。");
          return;
        }
        try {
          sessionStorage.setItem("seecript.lastScriptForSeo", rec.script.full_text);
        } catch (_) {}
        // 直接跳到 feature-3，进入页面后 bindSeoForm 检测到 sessionStorage 自动填入。
        window.location.href = "feature-3.html";
      });
    });
  }

  function renderKpis() {
    var kpis = getKpis();
    var fields = [
      ["scripts-week", kpis.scriptsThisWeek],
      ["personas-total", kpis.personasTotal],
      ["hours-saved", kpis.hoursSaved + "h"],
      // 兼容旧 selector data-kpi="skeletons-week"（workspace 旧版还可能挂着）
      ["skeletons-week", kpis.scriptsThisWeek],
    ];
    fields.forEach(function (pair) {
      var el = document.querySelector('[data-kpi="' + pair[0] + '"]');
      if (el) el.textContent = String(pair[1]);
    });
  }

  /**
   * 把 detail 里的「✎ 编辑」按钮接到 SeecriptPersonaEditor。
   *
   * 为什么不在 renderPersonasBoard 内部直接绑：
   *   - bindPersonaEditButtons 是幂等的（dataset.kocBound 锁），renderBoards 多次调用都安全；
   *   - 与 bindToggleButtons / bindDeleteButtons 是同一种装配模式（grep 一致性）。
   *   - PersonaEditor 是 SOLID/DIP 边界——SeecriptHistory 不知道它怎么实现 modal，
   *     只在 window.SeecriptPersonaEditor 存在时调用；不存在就 no-op（CLI 测试场景）。
   */
  function bindPersonaEditButtons() {
    document.querySelectorAll('[data-action="edit-persona"]').forEach(function (btn) {
      if (btn.dataset.kocBound === "1") return;
      btn.dataset.kocBound = "1";
      btn.addEventListener("click", function () {
        var recordId = btn.dataset.recordId;
        var idx = parseInt(btn.dataset.personaIdx, 10);
        var persona = getPersona(recordId, idx);
        if (!persona) {
          if (window.SeecriptApi && window.SeecriptApi.showToast) {
            SeecriptApi.showToast("找不到该方案，可能已被删除。", "error");
          } else {
            alert("找不到该方案，可能已被删除。");
          }
          return;
        }
        if (!window.SeecriptPersonaEditor || typeof window.SeecriptPersonaEditor.open !== "function") {
          alert("编辑器未加载（请刷新页面）。");
          return;
        }
        window.SeecriptPersonaEditor.open(persona, {
          title: "编辑方案 · " + (persona.name || "未命名"),
          onSave: function (patch) {
            var updated = updatePersona(recordId, idx, patch);
            if (!updated) return false;
            // 重渲染整个看板——简单可靠，因为本浏览器存量记录上限 30 条。
            renderBoards();
            if (window.SeecriptApi && window.SeecriptApi.showToast) {
              SeecriptApi.showToast("已保存修改：" + (updated.name || "未命名"), "success");
            }
            return true;
          },
        });
      });
    });
  }

  function renderBoards() {
    var pUl = document.querySelector('[data-history-list="personas"]');
    var sUl = document.querySelector('[data-history-list="scripts"]');
    // 兼容旧 selector：workspace 老版 HTML 用的是 "skeletons"
    if (!sUl) sUl = document.querySelector('[data-history-list="skeletons"]');
    if (pUl) renderPersonasBoard(pUl);
    if (sUl) renderScriptsBoard(sUl);
    bindToggleButtons();
    bindDeleteButtons();
    bindScriptActions();
    bindPersonaEditButtons();
    renderKpis();
  }

  // ---- public surface -----------------------------------------------------
  window.SeecriptHistory = {
    savePersonas: savePersonas,
    saveScript: saveScript,
    listPersonas: listPersonas,
    listScripts: listScripts,
    getPersona: getPersona,
    updatePersona: updatePersona,
    removePersona: removePersona,
    removeScript: removeScript,
    renderBoards: renderBoards,
    getKpis: getKpis,
  };

  document.addEventListener("DOMContentLoaded", renderBoards);
})();
