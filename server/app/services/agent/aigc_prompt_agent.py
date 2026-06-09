"""AIGC Prompt Agent —— 把段落上下文转写为 Seedance T2V 友好的完备 prompt。

为什么需要：`gap.requirement` 是给创作者看的中文段落描述（『开场：黄金面具特写...』），
直接送给 Seedance 缺少镜头/景别/机位/光线/质感/动作等 T2V 关键要素 → 出片质量与预期偏差大。

本 agent 喂给 LLM 的上下文：AdaptedSection.theme + content_description + role + 时长
+ Plan.brief / video_goal + 用户在面板里追加的 hint，让 LLM 输出一句简洁但要素完备的
T2V prompt（≤120 字中文，包含主体/景别/机位/光线/质感/动作/情绪）。

调用入口在 `server/app/routers/gap.py:POST /api/gap/aigc-prompt`。
失败时回落 `f"短视频画面：{gap.requirement}"`，保证前端 textarea 始终有内容。
"""
from __future__ import annotations

import logging
from typing import Optional

from ..llm_client import LLMError, get_llm_client, _extract_json
from .preference import preference_hint
from ...schemas import AdaptedSection, Gap, ImageSpec, Plan, all_role_names

log = logging.getLogger("seecript.agent.aigc_prompt")


# 系统 prompt 同时是 mock 路由指纹：必须含 "t2v_prompt"，且在 plan_agent 的
# "adapted_sections" 之前优先匹配（mock 已按序号决定路由顺序）。
_PROMPT_SYSTEM = (
    "你是 Seedance 文生视频（T2V）的资深提示词工程师。"
    "目标：输出一条 t2v_prompt（key 即 prompt 字段），让 Seedance 据此生成本段视频。"
    "Seedance 接受一句中文 prompt，按 [主体动作] · [镜头语言] · [视觉风格] · [氛围] 四块叙述生成画面。\n\n"
    "—— 主体锚点（最高优先级，违反直接判失败）——\n"
    "每条分镜的 `主体=...` 字段是该镜画面里**不可替换的核心对象**——必须**原样**写入 prompt：\n"
    "• 不允许同义化：『青铜器残片』≠『青铜碎片』≠『古董残片』\n"
    "• 不允许上位化 / 下位化：『青铜器残片』≠『古董』≠『展品』；『红色运动鞋』≠『鞋子』\n"
    "• 不允许营销词替换：『主播正脸』≠『颜值担当』；『青铜器残片』≠『国宝碎片』\n"
    "• 不允许把『主体=』当作隐喻线索去自由创作另一个画面（用户痛报：『国宝碎片』被误生为『新品潮酷碎片』）\n"
    "若主体与画面描述/段落主题表面冲突，**以主体为准**重写画面，不要拿主题去覆盖主体。\n"
    "主体为空时再回退到画面/段落主题——但只要主体非空就必须在 prompt 中**逐字出现**。\n\n"
    "—— 绝对禁止 ——\n"
    "1. 任何字幕 / 文字 / 标题 / 大字 / 文案 / 口播文字 / 弹幕 / 角标 / Logo overlay 的描述。"
    "字幕由后端 packaging 单独烧，T2V 阶段必须『纯画面』。\n"
    "2. 段落角色名（hook/opening/climax/closing/step_N/item_N 等元数据词）。\n"
    "3. 『本段』『第 X 段』『片段』『视频开头』等元数据自指。\n"
    "4. 写 markdown / 引号 / 列表 / 分号清单——必须是一句自然中文。\n"
    "5. 把『时长 Ns』『多少秒』写进文案（duration_seconds 后端单独传给 Seedance）。\n\n"
    "—— 必须覆盖（融进一句话，不要分号罗列）——\n"
    "• 主体：画面里的核心人/物 + 在做什么动作（动词必须具体，避免『展示』『呈现』空动词）\n"
    "• 镜头语言：景别（特写/中近景/中景/全景/大全景/航拍）+ 机位运动（固定/缓推/快推/跟随/环绕/手持/俯仰摇移）\n"
    "• 视觉风格：光线（自然光/聚光灯/逆光/低位顶光/霓虹/烛光/暮光）+ 色调（高对比冷调/暖金/低饱和胶片/赛博霓虹）+ 质感（电影感、35mm 胶片、产品级精修、纪实手持、4K 高清、动态模糊、景深）\n"
    "• 氛围：神秘/庄重/紧张/治愈/燃情/疏离/华丽/克制——选 1 个不要堆\n\n"
    "—— 风格范式 ——\n"
    "Good：『一只青瓷瓶在展柜中央静置，镜头缓慢环绕推进，顶部聚光灯打在釉面上折射冷白光斑，"
    "背景渐隐入纯黑，35mm 胶片质感的暖金调与冷光对比强烈，氛围庄重神秘』\n"
    "Bad：『展厅文物特写，强构图近景特写景别，机位快速摇移，光线高对比,电影感画质，"
    "冲击力强』（要素罗列、空动词、缺主体、空泛形容词堆砌）\n\n"
    "—— 输出 ——\n"
    "返回 JSON：{\"prompt\": \"...60-120 字一句完整自然中文...\", \"thinking\": "
    "[\"识别本段核心主体...\", \"决定镜头语言...\", \"选定光线色调与氛围...\"]}\n"
    "thinking 是 2-4 条短句（每条 ≤30 字），讲清你是怎么从段落上下文推到最终 prompt 的，"
    "用于在前端展示『agent 思考过程』。"
)


async def generate_aigc_prompt(
    gap: Gap,
    plan: Optional[Plan],
    section: Optional[AdaptedSection],
    *,
    user_hint: str = "",
) -> tuple[str, list[str]]:
    """根据 gap + 所属 section + plan 上下文 + 用户 hint 生成 T2V prompt。

    返回 `(prompt, thinking)`：
    - prompt：T2V 用最终一句中文
    - thinking：Agent 思考链，2-4 条短句，用于前端可视化

    失败兜底：回落到 `短视频画面：{gap.requirement}` + 单条思考说明走兜底。
    """
    hint = (user_hint or "").strip()[:200]
    role = section.role if section else gap.section
    theme = (section.theme if section else "") or "（无主题）"
    content_desc = (section.content_description if section else "").strip()
    duration = float(section.duration_seconds) if section else 4.0

    brief = (plan.brief or "").strip() if plan else ""
    goal = (plan.video_goal or "").strip() if plan else ""
    settings = plan.settings if plan else None

    user_lines: list[str] = [
        f"段落角色：{role}",
        f"段落主题：{theme}",
        f"段落时长：约 {duration:.1f}s",
        f"段落内容说明：{content_desc or '（无）'}",
        f"原始槽位需求：{gap.requirement}",
    ]
    if brief:
        user_lines.append(f"视频整体主题：{brief}")
    if goal:
        user_lines.append(f"视频要求与目的：{goal}")
    if settings is not None:
        user_lines.append(preference_hint(settings.migration_preference))
    frame = getattr(settings, "frame_design", None) if settings else None
    if frame is not None:
        fd_parts: list[str] = []
        if frame.preset and frame.preset != "custom":
            fd_parts.append(f"预设={frame.preset}")
        if frame.motion_density and frame.motion_density != "balanced":
            fd_parts.append(f"动效密度={frame.motion_density}")
        if frame.palette:
            fd_parts.append(f"色板={'/'.join(frame.palette[:3])}")
        if frame.grain_overlay:
            fd_parts.append("胶片颗粒")
        if frame.vignette:
            fd_parts.append("暗角")
        if frame.notes:
            fd_parts.append(f"备注={frame.notes}")
        if fd_parts:
            user_lines.append("frame.md 设计系统：" + " | ".join(fd_parts) + "（画面色调/质感/构图需贴合）")
    if hint:
        user_lines.append(f"创作者额外提示：{hint}")
    # stage-24：把 ShotPlan 显式告知 LLM，让它在 T2V prompt 里覆盖每个分镜的主体/画面
    # stage-25：若分镜带 targets，按目标分布提示 T2V prompt 须覆盖每个目标
    shots = getattr(section, "shots", None) or [] if section else []
    if shots:
        shot_lines = ["本段分镜清单（请把每一镜的画面要素融进同一句 T2V prompt）："]
        for sh in shots:
            subj = (sh.subject or "").strip()
            base = (
                f"  · 分镜 #{sh.order+1}（{sh.duration_seconds:.1f}s）"
                f" 主体={subj or '—'}{'（锚点·原样写入 prompt）' if subj else ''} | 画面={sh.visual or '—'}"
            )
            tgts = getattr(sh, "targets", None) or []
            if tgts:
                tgt_str = "; ".join(
                    f"{t.kind}-{t.name}" + (f"({t.role})" if t.role else "")
                    + (f"[{t.visual_hint}]" if t.visual_hint else "")
                    for t in tgts
                )
                base += f" | 目标={tgt_str}"
            cam = (getattr(sh, "camera_technique", "") or "").strip()
            if cam:
                base += f" | 运镜={cam}（必须在 prompt 中体现）"
            shot_lines.append(base)
        user_lines.append("\n".join(shot_lines))
    user_lines.append(
        "请输出一句完备的 t2v_prompt，覆盖主体/景别/机位/光线/质感/情绪；"
        "若分镜带『运镜』字段，必须把该运镜短语原样融入 prompt（这是渲染端的硬约束）。"
    )

    user = "\n".join(user_lines)

    llm = get_llm_client()
    try:
        text = await llm.complete(_PROMPT_SYSTEM, user)
        data = _extract_json(text) if text else None
        prompt = ""
        thinking: list[str] = []
        if isinstance(data, dict):
            prompt = str(data.get("prompt") or "").strip()
            raw_thinking = data.get("thinking")
            if isinstance(raw_thinking, list):
                thinking = [str(x).strip()[:60] for x in raw_thinking if str(x).strip()][:4]
        prompt = _sanitize(prompt)
        if prompt:
            log.info(
                "[aigc-prompt] gap=%s role=%s ok len=%d think=%d",
                gap.gap_id, role, len(prompt), len(thinking),
            )
            return prompt, thinking
        log.warning("[aigc-prompt] gap=%s LLM 返回空 prompt → fallback", gap.gap_id)
    except (LLMError, ValueError, Exception) as exc:  # noqa: BLE001
        log.warning("[aigc-prompt] gap=%s LLM 失败 → fallback：%s", gap.gap_id, exc)

    fb = _fallback_prompt(gap, section, hint)
    return fb, ["LLM 暂时不可用，使用本地兜底拼装", "已合并段落内容与镜头默认模板"]


def _sanitize(prompt: str) -> str:
    """裁剪长度 + 去掉 prompt 里偶发的 markdown 残留与角色元数据词。"""
    if not prompt:
        return ""
    import re as _re
    s = prompt.replace("```", "").replace("`", "").strip()
    # 不允许 5 模式下任何角色名直接出现（包含 step/item 通配前缀）
    for bad in all_role_names():
        s = s.replace(bad, "")
    # 剥离动态序号 step_N / item_N 残留
    s = _re.sub(r"\bstep[_\s]?\d+\b", "", s, flags=_re.IGNORECASE)
    s = _re.sub(r"\bitem[_\s]?\d+\b", "", s, flags=_re.IGNORECASE)
    # 截断到 200 字（system prompt 要求 60-120，但留余地处理 LLM 越界）
    if len(s) > 200:
        s = s[:200].rstrip("，。；,;.") + "…"
    return s.strip()


def _fallback_prompt(gap: Gap, section: Optional[AdaptedSection], hint: str) -> str:
    """LLM 失败时的本地合成：把 content_description / requirement / hint 拼成一句保底 prompt。"""
    parts: list[str] = []
    if section and section.content_description:
        parts.append(section.content_description.strip())
    if gap.requirement:
        parts.append(gap.requirement.strip())
    if hint:
        parts.append(hint)
    base = "；".join(p for p in parts if p) or f"短视频画面：{gap.section} 段"
    # 兜底加点拍摄要素，让 Seedance 不至于完全失焦
    return _sanitize(
        f"{base}。镜头建议：中景跟随，自然光，电影感色调，节奏与情绪贴合段落主题。"
    )


# =========================================================================
# 图片参考工作流（D2）：让 LLM 判断本段需要哪几张参考图
# =========================================================================

_IMAGE_SPEC_SYSTEM = (
    "你是 Seedance 文生视频的『参考图策展人 Agent』。给定一个段落的角色 / 主题 / 内容 / 时长 / 镜头目标，"
    "决定为这一段视频提前准备 1-4 张参考图（含首帧），让 Seedance 出片更稳。\n\n"
    "—— 主体锚点（最高优先级，违反直接判失败）——\n"
    "每条分镜的 `主体=...` 字段是该镜画面里**不可替换的核心对象**——必须**原样**写入对应参考图的 prompt：\n"
    "• 不允许同义化（『青铜器残片』≠『青铜碎片』）/ 上位化（『青铜器残片』≠『古董』）/ 营销化（『主播正脸』≠『颜值担当』）\n"
    "• 不允许把『主体』当作隐喻自由发挥（用户痛报：『国宝碎片』被误生成『新品潮酷碎片』）\n"
    "• 主体与画面/段落主题冲突时，**以主体为准**重写画面\n"
    "• caption 里也要点明主体的具象名词，不能用比喻\n\n"
    "—— 工作流 ——\n"
    "1. 先看段落主题与内容说明，识别画面里要出现的核心主体（人 / 物 / 场景）。\n"
    "2. **如果分镜带 targets 字段（比如带货视频一镜里『人物+商品』）→ 必须为每个 target 单独出 1 张图**，"
    "把目标个数作为参考图张数的下限：targets=2 至少 2 张图，targets=3 至少 3 张图。\n"
    "3. 没有 targets 时按段落信息密度：单主体过场 1 张；主体+环境 2 张；多主体并列 3 张。\n"
    "4. 为每张图设计互补构图：避免重复景别 / 机位 / 角度；多目标时每张图聚焦一个 target，"
    "在 caption 里点明这张图覆盖的是哪个 target（『主体特写：青瓷瓶』『辅体：展柜环境』）。\n\n"
    "—— 绝对禁止 ——\n"
    "1. 不许在 prompt 里描述任何字幕 / 标题 / 文案 / 大字 / 弹幕 / 角标 / Logo overlay——"
    "字幕由后端单独烧录，参考图只画纯画面。\n"
    "2. 不许出现段落角色名（hook/opening/climax/closing/step_N/item_N 等元数据词）。\n"
    "3. 不许『本段』『第 X 段』等元数据自指。\n"
    "4. caption 不要写『参考图 1』『第一张』，要写人话（『展厅入口仰拍』『海报特写』）。\n\n"
    "—— 字段规范 ——\n"
    "• slot_id：img-1 / img-2 / img-3 / img-4，按出现顺序编号\n"
    "• caption：≤30 字给创作者看的人话标签（『展厅入口仰拍』；多 target 时点明覆盖哪个 target）\n"
    "• prompt：60-120 字一句中文，给 Seedream 文生图直接消费——必须覆盖 [主体动作 · 镜头语言 · 视觉风格 · 氛围] 四块；"
    "若是 per-target 图，prompt 必须以该 target 的 name + visual_hint 为绝对主体；"
    "**对应分镜的『主体=』字段必须逐字出现在 prompt 中**\n"
    "• ratio：竖屏/抖音用 9:16，横屏/B 站用 16:9，方版用 1:1（按用户给的 default_ratio 兜底）\n\n"
    "—— 输出 JSON ——\n"
    "{\"specs\": [{\"slot_id\": \"img-1\", \"caption\": \"...\", \"prompt\": \"...\", \"ratio\": \"16:9\"}], "
    "\"thinking\": [\"识别本段核心主体...\", \"决定需要 N 张参考图，理由是...\", \"每张图的构图差异...\"]}\n"
    "thinking 是 2-4 条短句（每条 ≤30 字），讲清你怎么从段落上下文推到这套参考图方案，"
    "用于在前端展示『agent 思考过程』。"
)


async def generate_image_specs(
    gap: Gap,
    plan: Optional[Plan],
    section: Optional[AdaptedSection],
    *,
    user_hint: str = "",
    default_ratio: str = "16:9",
    subjects_override: Optional[list[str]] = None,
) -> tuple[list[ImageSpec], list[str]]:
    """根据 gap + section + plan 让 LLM 给出 1-3 张参考图建议。

    返回 `(specs, thinking)`：
    - specs：参考图清单
    - thinking：Agent 思考链，2-4 条短句，用于前端可视化

    `subjects_override`：前端从 plan.adapted_sections.shots[].subject 读出的具象名词清单。
    传入后会**覆盖** section.shots 上的 subject（避免 plan_store 与前端编辑态不同步），
    并在每张图返回前**强制注入** subject 锚点前缀——绕过 LLM 输出可能的同义化漂移。

    失败兜底：返回单张 `ImageSpec(slot-1, caption=段落主题, prompt=fallback)` + 兜底思考说明。
    """
    hint = (user_hint or "").strip()[:200]
    role = section.role if section else gap.section
    theme = (section.theme if section else "") or "（无主题）"
    content_desc = (section.content_description if section else "").strip()
    duration = float(section.duration_seconds) if section else 4.0

    brief = (plan.brief or "").strip() if plan else ""
    goal = (plan.video_goal or "").strip() if plan else ""
    settings = plan.settings if plan else None

    user_lines: list[str] = [
        f"段落角色：{role}",
        f"段落主题：{theme}",
        f"段落时长：约 {duration:.1f}s",
        f"段落内容说明：{content_desc or '（无）'}",
        f"原始槽位需求：{gap.requirement}",
        f"画幅默认：{default_ratio}",
    ]
    if brief:
        user_lines.append(f"视频整体主题：{brief}")
    if goal:
        user_lines.append(f"视频要求与目的：{goal}")
    if settings is not None:
        user_lines.append(preference_hint(settings.migration_preference))
    frame = getattr(settings, "frame_design", None) if settings else None
    if frame is not None:
        fd_parts: list[str] = []
        if frame.preset and frame.preset != "custom":
            fd_parts.append(f"预设={frame.preset}")
        if frame.palette:
            fd_parts.append(f"色板={'/'.join(frame.palette[:3])}")
        if frame.grain_overlay:
            fd_parts.append("胶片颗粒")
        if frame.notes:
            fd_parts.append(f"备注={frame.notes}")
        if fd_parts:
            user_lines.append("frame.md 设计系统：" + " | ".join(fd_parts) + "（参考图色调与质感需贴合）")
    if hint:
        user_lines.append(f"创作者额外提示：{hint}")
    # stage-26 PR-O：前端从 plan.adapted_sections.shots[].subject 取出的主体清单是
    # **最高优先级**——如果与 section.shots 落库版本不一致，以前端为准（避免编辑未同步）。
    authoritative_subjects: list[str] = []
    if subjects_override:
        authoritative_subjects = [
            str(s).strip()[:40] for s in subjects_override if str(s).strip()
        ][:4]
    if authoritative_subjects:
        user_lines.append(
            "—— 主体锚点清单（最高优先级，必须在每张图的 prompt 里逐字出现，禁同义化/上位化/营销化）——\n"
            + "\n".join(f"  · 主体 #{i+1}：{s}" for i, s in enumerate(authoritative_subjects))
        )
    # stage-24：若 plan_agent 已给出 ShotPlan 拆分，让 image-spec 一镜一图严格对齐
    # stage-25：若分镜带 targets，每个 target 一张图；caption 点明覆盖目标
    shots = getattr(section, "shots", None) or [] if section else []
    if shots:
        total_targets = sum(len(getattr(sh, "targets", None) or []) for sh in shots)
        if total_targets > 0:
            shot_lines = [
                f"本段已被拆为 {len(shots)} 个分镜，共 {total_targets} 个 target。"
                f"请按【每个 target 一张图】输出（specs 数量 ≥ target 总数，上限 4）："
            ]
        else:
            shot_lines = [
                f"本段已被拆为 {len(shots)} 个分镜，请按一镜一图输出（specs 数量等于 N，slot_id 与分镜序号对应）："
            ]
        for i, sh in enumerate(shots):
            # 前端 override 的主体优先（避免落库的 ShotPlan.subject 与编辑态不一致）
            subj = (
                authoritative_subjects[i]
                if i < len(authoritative_subjects)
                else (sh.subject or "").strip()
            )
            base = (
                f"  · 分镜 #{sh.order+1}（{sh.duration_seconds:.1f}s）"
                f" 主体={subj or '—'}{'（锚点·原样写入 prompt）' if subj else ''} | 画面={sh.visual or '—'}"
            )
            tgts = getattr(sh, "targets", None) or []
            if tgts:
                tgt_str = "; ".join(
                    f"{t.kind}-{t.name}" + (f"({t.role})" if t.role else "")
                    + (f"[{t.visual_hint}]" if t.visual_hint else "")
                    for t in tgts
                )
                base += f" | 目标={tgt_str}"
            cam = (getattr(sh, "camera_technique", "") or "").strip()
            if cam:
                base += f" | 运镜={cam}（图也要体现该机位/景别/构图）"
            shot_lines.append(base)
        user_lines.append("\n".join(shot_lines))
    user_lines.append("请输出 1-4 张参考图的 specs JSON（带 targets 时按 target 个数出图）。")

    user = "\n".join(user_lines)

    llm = get_llm_client()
    try:
        text = await llm.complete(_IMAGE_SPEC_SYSTEM, user)
        data = _extract_json(text) if text else None
        thinking: list[str] = []
        if isinstance(data, dict):
            raw_thinking = data.get("thinking")
            if isinstance(raw_thinking, list):
                thinking = [str(x).strip()[:60] for x in raw_thinking if str(x).strip()][:4]
        if isinstance(data, dict) and isinstance(data.get("specs"), list):
            specs: list[ImageSpec] = []
            for i, raw in enumerate(data["specs"][:4]):
                if not isinstance(raw, dict):
                    continue
                caption = str(raw.get("caption") or "").strip()[:80]
                prompt = _sanitize(str(raw.get("prompt") or ""))[:300]
                ratio = str(raw.get("ratio") or default_ratio).strip() or default_ratio
                if not caption or not prompt:
                    continue
                slot_id = str(raw.get("slot_id") or f"img-{i+1}").strip()[:32]
                # 后处理硬锚点：第 i 张图按主体清单第 i 个匹配；若 prompt 里没逐字包含
                # 该主体（LLM 同义化漂移），强制前缀注入。这一步是"在写入数据库前"的最后保险，
                # 保证下游 Seedream 一定能在 prompt 里看到具象主体词。
                target_subject = (
                    authoritative_subjects[i] if i < len(authoritative_subjects) else ""
                )
                if target_subject and target_subject not in prompt:
                    prompt = (
                        f"[必须画出且不可替换的主体：{target_subject}（禁同义化/上位化/营销化）] "
                        + prompt
                    )[:300]
                specs.append(ImageSpec(slot_id=slot_id, caption=caption, prompt=prompt, ratio=ratio))
            if specs:
                log.info(
                    "[image-spec] gap=%s role=%s ok n=%d think=%d",
                    gap.gap_id, role, len(specs), len(thinking),
                )
                return specs, thinking
        log.warning("[image-spec] gap=%s LLM 返回不合法 → fallback", gap.gap_id)
    except (LLMError, ValueError, Exception) as exc:  # noqa: BLE001
        log.warning("[image-spec] gap=%s LLM 失败 → fallback：%s", gap.gap_id, exc)

    # Fallback：单张图，prompt 走通用兜底
    caption = (section.theme if section else "") or gap.requirement[:30] or f"{role} 段参考图"
    prompt = _fallback_prompt(gap, section, hint)
    fb_specs = [
        ImageSpec(
            slot_id="img-1",
            caption=caption[:80],
            prompt=prompt[:300],
            ratio=default_ratio,
        )
    ]
    fb_thinking = [
        "LLM 暂时不可用，使用本地兜底",
        "默认 1 张参考图覆盖段落主题",
    ]
    return fb_specs, fb_thinking
