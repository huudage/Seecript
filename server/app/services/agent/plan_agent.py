"""结构改编 Agent —— 把样例的段落骨架按用户主题 + 视频目的改编成新结构。

数据流（与本期 Compose 升级配套）：
1. decompose_agent 已经把样例真模型拆成了 manifest.sections（含 role + theme + shot_indices）
2. 用户在 Compose 页填 brief（主题/卖点） + video_goal（视频要求与目的）
3. 本 agent 把"样例骨架 + 用户意图"喂给 LLM，让它**改编**而不是照抄
   - 允许：增/删/合并/重排段落
   - 硬约束：首=opening、末=closing、≤1 climax、中间皆 development、总段数 3-7
4. 每段除了 role/theme，额外产出 `content_description`——告诉创作者本段画面/口播该呈什么
5. 落地为 list[AdaptedSection]，section_id = f"sec-{order}"，gap_agent 据此分槽位

为什么需要：旧 plan/build 硬编码 5 段把 manifest 直接丢掉，所有视频都被压成同模板。
新版让样例的真实结构成果（已经过真模型校准）成为改编基线，再用 LLM 二次创作贴合用户需求。
"""
from __future__ import annotations

import logging
import math
import re
from typing import Optional

from ..llm_client import get_llm_client, _extract_json
from ..assets import resolve_reference_image_urls
from .preference import preference_hint, analysis_hint
from ...schemas import (
    AdaptedSection,
    ComposeSettings,
    SampleManifest,
    Section,
    SectionRole,
    ShotPlan,
    ShotTarget,
    STRUCTURAL_PATTERNS,
    allowed_roles_for,
    role_is_closing,
    role_is_main,
    role_is_opening,
    role_is_peak,
)

log = logging.getLogger("seecript.agent.plan")


_ADAPT_SYSTEM = (
    "你是短视频结构改编师。给定 1-2 个参考样例视频的真实段落结构、视频画像（含 structural_pattern 与 tempo），"
    "以及创作者的主题、视频目的与创作设置，请把这些样例的『骨架』改编为本次新视频的段落结构。\n\n"
    "【stage-25 核心铁律 · 迁移结构 ≠ 抄内容】\n"
    "你要迁移的是样例的【结构】（段落角色 / 节奏 / 钩子-推进-高潮-收束的分配 / 镜头长度比例），"
    "**不是样例的具体视觉内容**。下面这些是『内容』，绝不可原样搬到新主题：\n"
    "- 样例里出现的具体物品（如『紫色莫比乌斯环』『毕业海报』『紫灰撞色背景』『某 logo』）\n"
    "- 样例的具体场景（如『毕业典礼现场』『校园』『画展』）\n"
    "- 样例特定的颜色配方 / 字体 / 动效图形（视为 graphic 类目标，需在新主题下重新设计）\n"
    "- 样例口播里的具体词汇 / 人名 / 校名 / 品牌名\n"
    "正确做法：从样例 Shot.targets 里只读『目标的类型分布与角色』——\n"
    "  · 样例镜头 = `[graphic: 紫色莫比乌斯环 primary, text: 展览大字 secondary]` →\n"
    "    结构特征 = 『1 个动态主图形 + 1 个主题大字』，可迁移；\n"
    "    迁移到『国家文物展』后应变成 `[object: 镇馆文物 primary, text: 展览大字 secondary]`，\n"
    "    **绝不允许保留紫色莫比乌斯环或紫灰撞色**。\n"
    "  · 样例的钩子节奏（如『开篇 2s 强动效拉眼球』）可迁移，但里面填什么必须按 brief 重写。\n\n"
    "若给了 2 个参考样例，请将它们作为对等的灵感来源，不必偏向某一份；可借用任一份的"
    "节奏、卡点与段落创意，但不要把两份段落简单拼接（最终段数仍受硬约束）。\n\n"
    "允许：增加段落、删除冗余段落、合并相邻段落、调整顺序。\n\n"
    "硬约束（按本次结构模式 pattern 决定）：\n"
    "1. 第一段 role 必须属于 pattern 的开场类\n"
    "2. 最后一段 role 必须属于 pattern 的收尾类\n"
    "3. 整支视频最多 1 段峰值类（无峰值类的模式则不出现）\n"
    "4. 中间段都不能是开场/收尾类\n"
    "5. 段数：listicle 模式 2-8 段，其他 3-7 段\n"
    "6. 所有段 duration_seconds 之和必须接近『目标总时长』（±20% 以内）\n\n"
    "每段返回字段：\n"
    "- role: 必须在合法 role 列表内；step_N/item_N 形式的从 1 开始按顺序编号\n"
    "- theme: 中文短标签（≤8 字），紧贴创作者主题，不照抄样例\n"
    "- content_description: 内容说明（30-100 字）—— 告诉创作者画面该呈现什么、"
    "口播该说什么、为什么放在这个位置，紧扣 brief + video_goal；若给了关键词，"
    "尽量自然融入；若给了 CTA，收尾段口播须体现。"
    "**重要**：必须明确指出本段画面的『主体』（人物/物品/场景），"
    "若有多个并列主体（如『青铜器、玉器、瓷器』）须显式列出，"
    "下游会据此自动拆成多个分镜——一个主体 = 一个分镜。"
    "**禁止**保留样例的具体物体/颜色名（『莫比乌斯环』『紫灰撞色』等），按 brief 重写。\n"
    "**分镜数量硬约束（stage-23 / E-PR 收敛）**：\n"
    "- 开场段（opening / intro / hook / establish / intro_scene / title_card）"
    "和收尾段（closing / recap / closer / resolve / wrap_up / payoff）："
    "**默认 1 个分镜，最多 2 个**（结构骨架段镜头过多会显得拖沓、节奏松散）。\n"
    "- 中间主体段（development / step_N / item_N / daily_N / flow / info_block 等）："
    "1-3 个分镜最佳，绝对不超过 5 个。\n"
    "- 单一主体 / 单一动作 / 简短表达 → 1 个分镜（如『主播口播一句开场』）\n"
    "- 2-3 个并列主体 / 一个动作的两三个机位 → 2-3 个分镜（仅限主体段，推荐区间）\n"
    "- 多于 5 个并列项时，合并相邻同类（如『青铜器、玉器、瓷器、漆器、织物』→ 合为『文物群像 / 器物特写 / 文物细节』3 镜）\n"
    "宁可少不要多——分镜过密会让段落割裂、口播追不上，且素材生成成本翻倍\n"
    "- adaptation_note: 改编理由（≤60 字）—— 说明本段相比样例做了什么调整（保留/合并/重排/新增），"
    "以及为什么这样做更贴合创作者需求；可空字符串\n"
    "- tempo: 节奏标签（slow/medium/fast/peak/deceleration 之一，可为 null）\n"
    "- duration_seconds: 本段时长（浮点秒）。开场/收尾 3-5s，峰值 5-10s，主体 4-8s；所有段之和贴近目标总时长\n"
    "- source_section_indices: 改编自原样例池哪些段落下标（合并后的 flat 下标）；纯新增段为 []\n"
    "- shots: **stage-24** 把本段按上面分镜数量约束拆成 1-3（最多 5）个分镜对象的数组。"
    "每个分镜对象字段：subject（≤8 字主体，如『主播』『青铜器』『展厅全景』）、"
    "**subject 必须是具象名词**——画面里实际拍到的人/物/场景本体；"
    "**严禁比喻、营销词、上位词**："
    "✅『青铜器残片』『主播正脸』『红色运动鞋』；"
    "❌『国宝碎片』（→『青铜器残片』）、『颜值担当』（→『主播正脸』）、『潮品』（→『红色运动鞋』）。"
    "下游文生图/文生视频会**原样**使用 subject 作为不可替换锚点，含义不清的比喻词会让生图跑偏。\n"
    "visual（≤80 字画面描述：主体+动作+构图+镜头语言；同样要用具象表达，subject 出现的词要原样保留）、"
    "narration（≤80 字本镜口播或字幕，纯画面镜头可空。**严禁为了凑时长复述同一个意思**——"
    "宁可短不许水；step3 阶段会按段长再做一次精确重写，所以这里给个简短初版即可）、"
    "duration_seconds（本镜时长，所有 shot 之和应等于本段 duration_seconds，"
    "单镜 1-15s）、"
    "**targets（stage-25 新增）**：本镜要呈现的目标分布数组（0-4 个，可空）。"
    "每个目标 = {kind: person/object/scene/text/graphic/other, name: ≤12 字短名, "
    "role: primary/secondary/background（可空，主体留 primary）, visual_hint: ≤40 字视觉特征（可空）}。"
    "**多目标镜必须分开列**（如带货镜含 `[人物-主播, 物品-商品]`，文物展含 `[物品-文物, 文字-展名]`）。"
    "下游 aigc 补齐会按 targets 数量并行出 N 张图再喂 T2V，单目标镜走老路。\n\n"
    "返回 JSON：{\"adapted_sections\": [{\"role\": str, \"theme\": str, "
    "\"content_description\": str, \"adaptation_note\": str, \"tempo\": str|null, "
    "\"duration_seconds\": number, \"source_section_indices\": [int], "
    "\"shots\": [{\"subject\": str, \"visual\": str, \"narration\": str, \"duration_seconds\": number, "
    "\"targets\": [{\"kind\": str, \"name\": str, \"role\": str|null, \"visual_hint\": str|null}]}]}]}"
)


_ALLOWED_ROLES: set[SectionRole] = {"opening", "development", "climax", "closing"}

# 每段时长硬区间（与 schema AdaptedSection.duration_seconds 的 ge/le 对齐）
_MIN_SEC = 2.0
_MAX_SEC = 30.0

# role → 默认时长（LLM 未给或 fallback 时用）
_DEFAULT_DURATION: dict[SectionRole, float] = {
    "opening": 4.0,
    "development": 6.0,
    "climax": 7.0,
    "closing": 4.0,
}

# Stage-16：按 role 的"类"给默认时长（开场/主体/峰值/收尾）；pattern-agnostic
_DEFAULT_DURATION_BY_CLASS: dict[str, float] = {
    "opening": 4.0,
    "main": 6.0,
    "peak": 7.0,
    "closing": 4.0,
}


# brief 里如果包含 ClarifyPanel 注入的「（涉及 X、Y、Z）」尾巴，或者「核心可拍物体：X、Y」
# 起头，直接抠出这些用户已经检查过的具象物体名词——作为 adapt_structure 的硬约束传给 LLM。
# 来源：clarify_agent._enforce_subjects_in_content 在 content 末尾补「（涉及 ...）」；
# ClarifyPanel.handleAdopt 在用户点采纳时也会把 brief_subjects + detected_subjects union
# 用同样的「（涉及 X、Y、Z）」格式拼到 outline.content，stitch_outline_to_brief 把 content
# 整段串进 brief。所以在这里做反向解析，把它们提取出来当 subject_anchors。
_BRIEF_SUBJECT_PATTERN = re.compile(r"（涉及([^）]+)）")
_BRIEF_SUBJECT_HEAD_PATTERN = re.compile(r"核心可拍物体[：:]([^\n。]+)")


def extract_subject_anchors(brief: Optional[str]) -> list[str]:
    """从用户 brief 文本里反解 ClarifyPanel 注入的具象物体名词。

    解析两种约定格式（都由 ClarifyPanel/clarify_agent 主动写入）：
    - `（涉及 X、Y、Z）` 后缀：可能出现多次，全部取并集
    - `核心可拍物体：X、Y、Z` 前缀（content 为空时的备用格式）

    去重保序，最多 8 个；长度 1-12 字（≥1 字防止误吞「品」「物」单字噪声）。
    """
    if not brief:
        return []
    out: list[str] = []
    seen: set[str] = set()
    blobs: list[str] = []
    blobs.extend(_BRIEF_SUBJECT_PATTERN.findall(brief))
    blobs.extend(_BRIEF_SUBJECT_HEAD_PATTERN.findall(brief))
    for blob in blobs:
        for raw in re.split(r"[、,，;；\s/／]+", blob):
            s = raw.strip()
            if not s or len(s) > 12:
                continue
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
            if len(out) >= 8:
                return out
    return out


def _default_duration_for(role: str, pattern: str) -> float:
    """按 role 在 pattern 中的类（opening/main/peak/closing）给默认时长。

    用 schemas.role_is_* 系列判定 role 的类。step_N/item_N 都归 main 类。
    """
    if role_is_opening(role, pattern):
        return _DEFAULT_DURATION_BY_CLASS["opening"]
    if role_is_closing(role, pattern):
        return _DEFAULT_DURATION_BY_CLASS["closing"]
    if role_is_peak(role, pattern):
        return _DEFAULT_DURATION_BY_CLASS["peak"]
    return _DEFAULT_DURATION_BY_CLASS["main"]

_PATTERN_DESCRIPTIONS: dict[str, str] = {
    "dramatic": "戏剧四段式（起承转合）：opening→development→climax→closing；首=opening、末=closing、≤1 段 climax",
    "stepwise": "线性步骤式（教程/操作）：intro→step_1→step_2→...→recap；首=intro、末=recap、无 climax 类",
    "listicle": "并列盘点式（榜单/N 个理由）：hook→item_1→item_2→...→closer；首=hook、末=closer、无 climax 类，段数可放宽到 2-8",
    "atmospheric": "氛围推进式（Vlog/纪录）：establish→flow→peak→resolve；首=establish、末=resolve、≤1 段 peak",
    "info_dense": "信息密集快切式：title_card→info_block→...→payoff；首=title_card、末=payoff、无独立峰值类",
}


def _build_pattern_hint(pattern: str, allowed_roles: list[str]) -> str:
    """给 LLM 描述当前 pattern 的角色体系 + 段数硬约束。"""
    desc = _PATTERN_DESCRIPTIONS.get(pattern, _PATTERN_DESCRIPTIONS["dramatic"])
    seg_range = "2-8 段" if pattern == "listicle" else "3-7 段"
    role_list = "、".join(allowed_roles[:20])
    return (
        f"本次结构模式：{pattern}\n"
        f"模式说明：{desc}\n"
        f"合法 role 名（必须从中选取）：{role_list}\n"
        f"段数硬约束：{seg_range}"
    )


_TONE_LABEL: dict[str, str] = {
    "tight_hype": "紧凑高燃（快剪 + 强情绪，建议保留 climax）",
    "calm_narrative": "沉稳叙事（长镜头 + 余韵，climax 可选）",
    "casual_daily": "轻松日常（口语化 + 节奏自然）",
    "professional_cool": "专业冷静（信息密度高 + 弱情绪 + 重数据）",
}

_PLATFORM_LABEL: dict[str, str] = {
    "douyin": "抖音（9:16 竖屏，强字幕，节奏紧凑）",
    "wechat": "视频号（9:16 竖屏，节奏温和）",
    "xiaohongshu": "小红书（竖屏，文艺克制）",
    "bilibili": "B 站（16:9 横屏，叙事感）",
}


async def adapt_structure(
    manifests: list[SampleManifest],
    brief: Optional[str],
    video_goal: Optional[str],
    settings: Optional[ComposeSettings] = None,
    reference_asset_ids: Optional[list[str]] = None,
) -> list[AdaptedSection]:
    """改编 1-2 个参考样例的段落骨架成新结构。失败时回落到第一份样例的 1:1 拷贝。

    settings 注入目标总时长 / 平台 / 调性 / CTA / 关键词，驱动 LLM 分配每段 duration_seconds。
    reference_asset_ids 是用户素材库参考图/参考视频，喂多模态 LLM 做风格/调性/结构对齐。
    多样例时（len(manifests)==2）：两份 sections 被合并成一个 flat 参考池，行首加
    (样例A)/(样例B) tag 告诉 LLM 来源，但 LLM 输出的 source_section_indices 仍是
    合并后的 flat 下标，_materialize 直接 indexing 进 combined_sections。
    user payload 必须包含字面字符串 `原样例共 N 段`，让 mock 能 regex 解析段数。
    """
    if not manifests:
        log.warning("[plan-agent] manifests 为空，无法改编 → 空结构")
        return []
    if len(manifests) > 2:
        raise ValueError(f"adapt_structure 最多接受 2 个样例，收到 {len(manifests)}")

    settings = settings or ComposeSettings()
    target_total = float(settings.target_duration_seconds)

    # combined_sections: list[(global_idx, Section, manifest_idx)]
    combined_sections: list[tuple[int, Section, int]] = []
    for mi, manifest in enumerate(manifests):
        for sec in manifest.sections:
            combined_sections.append((len(combined_sections), sec, mi))
    n_src = len(combined_sections)
    if n_src == 0:
        log.warning("[plan-agent] 所有 manifests.sections 都为空 → fallback")
        return _fallback_adaptation(list(manifests[0].sections), target_total)

    brief_text = (brief or "").strip() or "（未提供主题）"
    goal_text = (video_goal or "").strip() or "（未提供具体目的）"
    cta_text = (settings.cta or "").strip() or "（未指定，可自拟收尾引导）"
    kw_text = "、".join(settings.keywords) if settings.keywords else "（无）"

    # 从 brief 里反解 ClarifyPanel 已经写入的具象物体清单——这是用户在澄清阶段
    # 亲自检查/编辑过的「可拍物体」白名单，下游 LLM 必须优先用这些当 shot.subject 和
    # ShotTarget.name，绝不准忽略掉。空列表表示用户没用 ClarifyPanel 或者 brief 没含物体。
    subject_anchors = extract_subject_anchors(brief)
    if subject_anchors:
        anchors_str = "、".join(subject_anchors)
        log.info("[plan-agent] subject_anchors from brief: %s", anchors_str)
    else:
        anchors_str = ""

    # 样例标签：单样例时不打 tag（行为与旧版一致）；多样例时打 (样例A)/(样例B)
    multi = len(manifests) > 1
    sample_lines: list[str] = []
    for global_idx, sec, mi in combined_sections:
        theme = sec.theme or "（无主题标签）"
        summary = (sec.summary or "").strip()[:60]
        shots = ",".join(str(idx) for idx in sec.shot_indices) or "-"
        tag = f"(样例{chr(ord('A') + mi)}) " if multi else ""
        sample_lines.append(
            f"[{global_idx}] {tag}role={sec.role} | theme={theme} | shots={shots} | summary={summary}"
        )

    # stage-25：样例 Shot.targets 频次摘要——只给 LLM 看『结构成分』（多少个 graphic/object/text 镜头），
    # 不传具体 name（避免 LLM 把『紫色莫比乌斯环』这种内容直接搬过来当结构）。
    target_kind_summary: list[str] = []
    for mi, manifest in enumerate(manifests):
        kind_counts: dict[str, int] = {}
        for sh in manifest.shots:
            for t in (sh.targets or []):
                kind_counts[t.kind] = kind_counts.get(t.kind, 0) + 1
        if not kind_counts:
            continue
        prefix = f"样例{chr(ord('A') + mi)} " if multi else ""
        parts = [f"{k}×{v}" for k, v in sorted(kind_counts.items(), key=lambda kv: -kv[1])]
        target_kind_summary.append(f"{prefix}样例镜头目标分布（结构成分参考，不含具体内容）：{', '.join(parts)}")
    target_summary_text = "\n".join(target_kind_summary) if target_kind_summary else ""

    # understanding：每份样例独立一段；缺失跳过
    understanding_blocks: list[str] = []
    for mi, manifest in enumerate(manifests):
        u = manifest.understanding
        if u is None:
            continue
        prefix = f"样例{chr(ord('A') + mi)} " if multi else ""
        understanding_blocks.append(
            f"{prefix}archetype：{u.archetype}\n"
            f"{prefix}narrative：{u.narrative_summary}\n"
            f"{prefix}tone：{u.tone}"
        )
    understanding_text = "\n\n".join(understanding_blocks) if understanding_blocks else "（无样例画像）"

    # 从第一份样例 understanding 取 structural_pattern（Stage-16 起 LLM 改编要按模式来）
    # 没有 understanding（老数据/缓存）则兜底 dramatic
    primary_understanding = manifests[0].understanding
    pattern = primary_understanding.structural_pattern if primary_understanding else "dramatic"
    allowed_roles_list = allowed_roles_for(pattern)
    pattern_hint = _build_pattern_hint(pattern, allowed_roles_list)

    user = (
        f"样例视频画像：\n{understanding_text}\n\n"
        f"创作者输入：\n"
        f"- 主题/卖点（brief）：{brief_text}\n"
        f"- 视频要求与目的（video_goal）：{goal_text}\n\n"
        + (
            f"【可拍物体白名单 · stage-34 硬约束】\n"
            f"用户在澄清阶段已经亲自检查/编辑过的具象物体清单：{anchors_str}\n"
            f"- 每段的 shots[].subject 必须**优先**从这个清单选；多段同名是允许的（不同段拍同一物体的不同侧面/状态）；\n"
            f"- shots[].targets 数组里至少有 1 个 target.name 命中清单（kind 给 object 或 person，按物体性质）；\n"
            f"- 整支视频里这个清单的物体**必须全部出现至少 1 次**（缺哪个就在合适的段里补一镜）；\n"
            f"- 清单之外的 subject 也允许（人物/场景补镜），但不准用上位词/营销词/形容词替换清单里的物体。\n\n"
            if anchors_str else ""
        )
        + f"创作设置：\n"
        f"- 目标总时长：{target_total:.0f}s\n"
        f"- 目标平台：{_PLATFORM_LABEL.get(settings.target_platform, settings.target_platform)}\n"
        f"- 整体调性：{_TONE_LABEL.get(settings.tone, settings.tone)}\n"
        f"- 核心 CTA：{cta_text}\n"
        f"- 必须出现的关键词：{kw_text}\n\n"
        f"{pattern_hint}\n\n"
        f"原样例共 {n_src} 段：\n" + "\n".join(sample_lines) + "\n\n"
        + (f"{target_summary_text}\n（注意：上述 graphic 类目标是样例的【动效形式特征】，"
           f"必须按本次 brief 的目标域重新设计——比如样例的『紫色莫比乌斯环』在文物展主题下"
           f"应替换为『镇馆文物的环绕展示』之类的同节奏图形，而不是搬同一个图形/同一个颜色。）\n\n"
           if target_summary_text else "")
        + f"请基于以上信息改编段落结构（"
        f"{'2-8' if pattern == 'listicle' else '3-7'} 段，遵守硬约束，"
        f"所有段时长之和贴近 {target_total:.0f}s）。"
    )

    # stage-23：迁移倾向 + 原片亮点/改进 注入到 user prompt 顶部，让 LLM 在改编时
    # 既保留原片强点、又规避其弱点，并按用户选的版本（情绪增强 / 节奏紧凑 / 平淡复刻）调倾向。
    pref_block = preference_hint(settings.migration_preference)
    analysis_block = "\n\n".join(
        block for block in (analysis_hint(m.analysis) for m in manifests) if block
    )
    leading_blocks = [pref_block]
    if analysis_block:
        leading_blocks.append(analysis_block)
    user = "\n\n".join(leading_blocks) + "\n\n" + user

    # 个性知识库注入（top-10 最近完成项目 + 用户手动启用的额外项目）。
    # 失败仅 warn 不阻 plan/build：profile 是增强、不是依赖。
    applied_rules_count = 0
    try:
        from ..profile import collect_active_rules, count_applied_rules, format_rules_for_prompt
        grouped = collect_active_rules()
        kb_text = format_rules_for_prompt(
            grouped, scopes=["structure", "pacing"], max_per_scope=6,
        )
        if kb_text:
            user = kb_text + "\n\n" + user
            applied_rules_count = count_applied_rules({k: grouped[k] for k in ("structure", "pacing") if k in grouped})
            log.info("[plan-agent] KB injected: rules=%d scopes=structure,pacing", applied_rules_count)
    except Exception as exc:  # noqa: BLE001
        log.warning("[plan-agent] KB 注入失败（跳过，不影响生成）: %s", exc)

    # 参考素材：用户在素材库选定的参考图/参考视频抽帧，作为视觉风格/构图/调性指引
    ref_images: list[str] = resolve_reference_image_urls(reference_asset_ids or [])
    if ref_images:
        user += (
            f"\n\n附带 {len(ref_images)} 张『参考画面』——它们不是样例视频的镜头，"
            f"而是用户希望本次新视频在风格/构图/调性上对齐的视觉参考。"
            f"改编 theme 和 content_description 时请隐式靠拢这些参考的视觉气质，"
            f"但不要把它们当成具体镜头来引用。"
        )

    llm = get_llm_client()
    try:
        if ref_images:
            text = await llm.complete_multimodal(_ADAPT_SYSTEM, user, ref_images)
        else:
            text = await llm.complete(_ADAPT_SYSTEM, user)
        data = _extract_json(text)
        raw = data.get("adapted_sections", []) if isinstance(data, dict) else []
        items = _parse_raw_items(raw, pattern)
        if items:
            items = _enforce_hard_constraints(items, n_src, pattern)
            items = _normalize_durations(items, target_total)
            sections = _materialize(items, combined_sections, pattern)
            # stage-34：硬注入 subject_anchors——LLM 经常会漏掉某些用户已确认的物体。
            # 用 _enforce_subject_anchors 兜底，把缺的 anchor 强行塞进最匹配的 section 的
            # ShotPlan.subject / ShotTarget，保证整支视频里每个 anchor 至少出现 1 次。
            if subject_anchors:
                sections = _enforce_subject_anchors(sections, subject_anchors)
            return sections
    except Exception as exc:
        log.warning("[plan-agent] adapt_structure LLM failed: %s → fallback", exc)

    fallback = _fallback_adaptation(list(manifests[0].sections), target_total, pattern)
    if subject_anchors:
        fallback = _enforce_subject_anchors(fallback, subject_anchors)
    return fallback


def _parse_raw_items(raw: list, pattern: str = "dramatic") -> list[dict]:
    """清洗 LLM 输出：保留合法 role（按 pattern 允许集校验）+ 截断超长字段。

    pattern 决定合法 role 集合（dramatic→4 元，stepwise/listicle 含 step_N/item_N 等）。
    LLM 偶尔会输出 'step1' 而非 'step_1'，这里做轻量正则容错。
    """
    import re as _re

    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    allowed = set(allowed_roles_for(pattern))
    valid_tempos: set[str] = {"slow", "medium", "fast", "peak", "deceleration"}
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip()
        # 容错：step1 → step_1、item02 → item_2
        role = _re.sub(r"^(step|item)\s*0*(\d+)$", r"\1_\2", role)
        if role not in allowed:
            continue
        theme = str(item.get("theme", "") or "").strip()[:20]
        content = str(item.get("content_description", "") or "").strip()[:300]
        if not content:
            continue
        adaptation_note = str(item.get("adaptation_note", "") or "").strip()[:60]
        tempo_raw = str(item.get("tempo", "") or "").strip().lower()
        tempo = tempo_raw if tempo_raw in valid_tempos else None
        src_idx_raw = item.get("source_section_indices", []) or []
        src_idx: list[int] = []
        if isinstance(src_idx_raw, list):
            for x in src_idx_raw:
                try:
                    src_idx.append(int(x))
                except (TypeError, ValueError):
                    continue
        default_dur = _default_duration_for(role, pattern)
        try:
            dur = float(item.get("duration_seconds") or default_dur)
        except (TypeError, ValueError):
            dur = default_dur
        dur = max(_MIN_SEC, min(_MAX_SEC, dur))

        # stage-24：解析 shots[]（LLM 没给时下游 plan.py 会兜底 1 镜）
        # E-PR 收敛：开场/收尾段最多 2 镜，主体段最多 5 镜
        is_edge = role_is_opening(role, pattern) or role_is_closing(role, pattern)
        shot_cap = 2 if is_edge else 5
        shots_raw = item.get("shots") or []
        shots_clean: list[dict] = []
        if isinstance(shots_raw, list):
            for sh in shots_raw[:shot_cap]:
                if not isinstance(sh, dict):
                    continue
                visual = str(sh.get("visual", "") or "").strip()[:200]
                if not visual:
                    continue
                subject = str(sh.get("subject", "") or "").strip()[:40]
                narration = str(sh.get("narration", "") or "").strip()[:200]
                try:
                    sh_dur = float(sh.get("duration_seconds") or 0)
                except (TypeError, ValueError):
                    sh_dur = 0.0
                if sh_dur <= 0:
                    sh_dur = 2.5
                sh_dur = max(1.0, min(15.0, sh_dur))
                shots_clean.append({
                    "subject": subject,
                    "visual": visual,
                    "narration": narration,
                    "duration_seconds": round(sh_dur, 2),
                })

        out.append({
            "role": role,
            "theme": theme,
            "content_description": content,
            "adaptation_note": adaptation_note,
            "tempo": tempo,
            "source_section_indices": src_idx,
            "duration_seconds": dur,
            "shots": shots_clean,
        })
    return out


def _normalize_durations(items: list[dict], target_total: float) -> list[dict]:
    """把每段 duration_seconds 归一化到 target_total 附近（±20% 内不动，超出按比例缩放再 clamp）。

    步骤：
    1. 每项已在 _parse_raw_items 中 clamp 到 [_MIN_SEC, _MAX_SEC]
    2. 计算总和；若与目标偏离 ≤20%，直接返回
    3. 否则按 target_total/current_total 比例缩放，再 clamp
    4. 如果 clamp 后偏差仍大，把残差均摊到非边界段（避免某段卡死在 clamp 后总和飘掉）
    """
    if not items or target_total <= 0:
        return items
    current = sum(float(it.get("duration_seconds") or 0.0) for it in items)
    if current <= 0:
        # 没有任何有效时长，按 role 默认值兜底
        for it in items:
            it["duration_seconds"] = _DEFAULT_DURATION.get(it.get("role"), 5.0)
        current = sum(it["duration_seconds"] for it in items)
    if current <= 0:
        return items
    deviation = abs(current - target_total) / target_total
    if deviation <= 0.2:
        return items
    scale = target_total / current
    for it in items:
        scaled = float(it["duration_seconds"]) * scale
        it["duration_seconds"] = max(_MIN_SEC, min(_MAX_SEC, scaled))
    # 残差均摊：clamp 后总和可能仍偏，按未触顶/触底的项均分一次
    new_total = sum(it["duration_seconds"] for it in items)
    delta = target_total - new_total
    if abs(delta) > 0.1:
        adjustable = [
            it for it in items
            if _MIN_SEC < it["duration_seconds"] < _MAX_SEC
        ]
        if adjustable:
            share = delta / len(adjustable)
            for it in adjustable:
                it["duration_seconds"] = max(
                    _MIN_SEC, min(_MAX_SEC, it["duration_seconds"] + share)
                )
    # 保留 1 位小数减少噪声
    for it in items:
        it["duration_seconds"] = round(float(it["duration_seconds"]), 1)
    return items


def _enforce_hard_constraints(items: list[dict], n_src: int, pattern: str = "dramatic") -> list[dict]:
    """强约束修正：首=opening 类、末=closing 类、中间不出现首尾类、≤1 峰值类、长度按 pattern。

    各 pattern 段数：listicle 2-8，其他 3-7。
    无 peak 类的模式（stepwise/listicle/info_dense）：中间段若是 peak 类被降级到 main 类。
    """
    if not items:
        return items

    n = len(items)

    def _opening_role() -> tuple[str, str]:
        slot = STRUCTURAL_PATTERNS.get(pattern, STRUCTURAL_PATTERNS["dramatic"])["opening"][0]
        if slot.endswith("_*"):
            slot = slot[:-2] + "_1"
        return slot, _default_theme(slot, pattern)

    def _closing_role() -> tuple[str, str]:
        slot = STRUCTURAL_PATTERNS.get(pattern, STRUCTURAL_PATTERNS["dramatic"])["closing"][0]
        if slot.endswith("_*"):
            slot = slot[:-2] + "_1"
        return slot, _default_theme(slot, pattern)

    def _main_role(idx: int = 1) -> str:
        slot = STRUCTURAL_PATTERNS.get(pattern, STRUCTURAL_PATTERNS["dramatic"])["main"][0]
        if slot.endswith("_*"):
            return slot[:-2] + f"_{idx}"
        return slot

    # 首段强制开场类
    if not role_is_opening(items[0].get("role", ""), pattern):
        new_role, new_theme = _opening_role()
        items[0]["role"] = new_role
        if not items[0].get("theme"):
            items[0]["theme"] = new_theme

    # 末段强制收尾类（n≥2）
    if n >= 2:
        if not role_is_closing(items[-1].get("role", ""), pattern):
            new_role, new_theme = _closing_role()
            items[-1]["role"] = new_role
            if not items[-1].get("theme"):
                items[-1]["theme"] = new_theme

    # 中间段：禁开场/收尾类；至多 1 个峰值；无 peak 模式则峰值降为 main
    pattern_def = STRUCTURAL_PATTERNS.get(pattern, STRUCTURAL_PATTERNS["dramatic"])
    has_peak_class = bool(pattern_def["peak"])
    peak_seen = 0
    main_counter = 1
    for i in range(1, n - 1):
        role = items[i].get("role", "")
        if role_is_opening(role, pattern) or role_is_closing(role, pattern):
            items[i]["role"] = _main_role(main_counter)
            main_counter += 1
        elif role_is_peak(role, pattern):
            if not has_peak_class:
                items[i]["role"] = _main_role(main_counter)
                main_counter += 1
            else:
                peak_seen += 1
                if peak_seen > 1:
                    items[i]["role"] = _main_role(main_counter)
                    main_counter += 1

    # 长度修正：listicle 2-8，其他 3-7
    min_seg = 2 if pattern == "listicle" else 3
    max_seg = 8 if pattern == "listicle" else 7
    if n < min_seg:
        return []
    if n > max_seg:
        kept: list[dict] = [items[0]]
        peak_item = next((it for it in items[1:-1] if role_is_peak(it.get("role", ""), pattern)), None)
        mains = [it for it in items[1:-1] if role_is_main(it.get("role", ""), pattern)]
        budget = max_seg - 2 - (1 if peak_item else 0)
        kept.extend(mains[:budget])
        if peak_item:
            kept.append(peak_item)
        kept.append(items[-1])
        items = kept

    return items


def _normalize_shot_durations(shots_raw: list[dict], section_total: float) -> list[ShotPlan]:
    """把每个 ShotPlan 的 duration_seconds 归一到 section 总时长。

    1. 总和与 section 总时长偏差 ≤ 10% 直接用
    2. 否则按比例缩放，clamp 到 [1.0, 15.0]
    3. 残差均摊到非边界镜
    返回 list[ShotPlan]；shots_raw 为空时返回 []。
    """
    if not shots_raw:
        return []
    n = len(shots_raw)
    cur = sum(float(s.get("duration_seconds") or 0) for s in shots_raw) or 1.0
    if cur > 0:
        ratio = section_total / cur
        for s in shots_raw:
            d = float(s.get("duration_seconds") or 2.5) * ratio
            s["duration_seconds"] = max(1.0, min(15.0, d))
    new_total = sum(float(s["duration_seconds"]) for s in shots_raw)
    delta = section_total - new_total
    if abs(delta) > 0.1:
        adjustable = [s for s in shots_raw if 1.0 < s["duration_seconds"] < 15.0]
        if adjustable:
            share = delta / len(adjustable)
            for s in adjustable:
                s["duration_seconds"] = max(1.0, min(15.0, s["duration_seconds"] + share))

    out: list[ShotPlan] = []
    for order, s in enumerate(shots_raw):
        # stage-25：解析 LLM 给的 targets（可空）
        raw_targets = s.get("targets") or []
        parsed_targets: list[ShotTarget] = []
        if isinstance(raw_targets, list):
            for t in raw_targets[:4]:
                if not isinstance(t, dict):
                    continue
                name = str(t.get("name") or "").strip()[:24]
                if not name:
                    continue
                kind = str(t.get("kind") or "object").strip().lower()
                if kind not in ("person", "object", "scene", "text", "graphic", "other"):
                    kind = "other"
                role_val = t.get("role")
                tgt_role: Optional[str] = None
                if isinstance(role_val, str) and role_val in ("primary", "secondary", "background"):
                    tgt_role = role_val
                hint = t.get("visual_hint")
                hint = str(hint).strip()[:80] if isinstance(hint, str) and hint.strip() else None
                try:
                    parsed_targets.append(ShotTarget(kind=kind, name=name, role=tgt_role, visual_hint=hint))  # type: ignore[arg-type]
                except Exception:  # noqa: BLE001
                    continue
        out.append(ShotPlan(
            order=order,
            subject=s.get("subject", "") or "",
            visual=s.get("visual", "") or "",
            narration=s.get("narration", "") or "",
            duration_seconds=round(float(s.get("duration_seconds") or 2.5), 2),
            targets=parsed_targets,
        ))
    return out


def _auto_shots_for_section(section_total: float, content_description: str, role: str) -> list[ShotPlan]:
    """plan_agent 没给 shots[] 时（旧 LLM/缓存）的兜底：1 镜包整段。

    保持向后兼容：plan.py 物化时若 shots 为空会调本函数生成默认 1 镜。
    """
    return [ShotPlan(
        order=0,
        subject="",
        visual=(content_description or f"本段（{role}）画面")[:200],
        narration="",
        duration_seconds=round(max(1.0, min(15.0, section_total)), 2),
    )]


def _enforce_subject_anchors(
    sections: list[AdaptedSection], anchors: list[str]
) -> list[AdaptedSection]:
    """硬注入 subject_anchors——LLM 漏掉某个用户确认的物体时，机械补回去。

    判定缺失：anchor 没出现在任何 shot.subject、targets.name、shot.visual、
    section.content_description 里（精确子串匹配）。
    补法：把缺的 anchor 塞进**主体段**（development/step_N/item_N/flow/info_block 等
    role_is_main 命中的段）的中段第一个 shot——优先复用现有 shot，把其 subject 改写
    为 anchor、targets 头部插一个 kind="object" 的 ShotTarget；如果没有 shots 则用
    占位 ShotPlan 兜底。开场/收尾段不动（节奏铁律）。

    锚点全部命中则原样返回。
    """
    if not sections or not anchors:
        return sections

    # 命中检测：把所有可能写到的字段拼成大字符串
    blob_parts: list[str] = []
    for sec in sections:
        if sec.content_description:
            blob_parts.append(sec.content_description)
        for sh in (sec.shots or []):
            if sh.subject:
                blob_parts.append(sh.subject)
            if sh.visual:
                blob_parts.append(sh.visual)
            if sh.narration:
                blob_parts.append(sh.narration)
            for t in (sh.targets or []):
                if t.name:
                    blob_parts.append(t.name)
    blob = "\n".join(blob_parts)
    missing = [a for a in anchors if a and a not in blob]
    if not missing:
        return sections

    # 选可注入的主体段（去掉首段和末段，且不是首/末段 role）
    n = len(sections)
    candidate_idxs: list[int] = []
    for i, sec in enumerate(sections):
        if i == 0 or i == n - 1:
            continue
        candidate_idxs.append(i)
    if not candidate_idxs:
        # 只有 1-2 段的极端情况，退而求其次：用倒数第二段（或唯一段）
        candidate_idxs = [max(0, n - 2)]

    log.info(
        "[plan-agent] _enforce_subject_anchors missing=%s targets=%d sections=%d",
        missing, len(candidate_idxs), n,
    )

    # 轮转分配：每个 anchor 塞到下一个候选段
    cursor = 0
    for anchor in missing:
        sec_idx = candidate_idxs[cursor % len(candidate_idxs)]
        cursor += 1
        sec = sections[sec_idx]
        shots = list(sec.shots or [])
        if not shots:
            # 空 shots：补一个新 shot（duration 取 section 总时长的 60% 但 ≤ 8s ≥ 1s）
            base_dur = max(1.0, min(8.0, float(sec.duration_seconds or 4.0) * 0.6))
            new_shot = ShotPlan(
                order=0,
                subject=anchor,
                visual=f"{anchor}的特写镜头",
                narration="",
                duration_seconds=round(base_dur, 2),
                targets=[ShotTarget(kind="object", name=anchor, role="primary", visual_hint=None)],
            )
            shots = [new_shot]
        else:
            # 改写中段第一个 shot 的 subject + targets——保留它原本的 visual/narration/duration
            mid = len(shots) // 2
            target_shot = shots[mid]
            new_targets = list(target_shot.targets or [])
            # 看 anchor 是否已在 targets.name；不在则插到最前
            if not any(t.name == anchor for t in new_targets):
                new_targets.insert(
                    0,
                    ShotTarget(kind="object", name=anchor, role="primary", visual_hint=None),
                )
            # 保留原 visual，但如果 anchor 不在 visual 里就拼上
            new_visual = target_shot.visual or ""
            if anchor not in new_visual:
                new_visual = (anchor + "：" + new_visual)[:200] if new_visual else f"{anchor}的特写镜头"
            shots[mid] = target_shot.model_copy(
                update={
                    "subject": anchor if not target_shot.subject or target_shot.subject not in anchors else target_shot.subject,
                    "visual": new_visual,
                    "targets": new_targets[:4],
                }
            )
        # section.content_description 也拼一下 anchor 名字（前端拆解卡片读它）
        new_content = sec.content_description or ""
        if anchor not in new_content:
            suffix = f"（含{anchor}）"
            new_content = (new_content + suffix)[:400] if new_content else f"主体：{anchor}"
        sections[sec_idx] = sec.model_copy(
            update={"shots": shots, "content_description": new_content},
        )

    return sections


def _materialize(
    items: list[dict],
    combined_sections: list[tuple[int, Section, int]],
    pattern: str = "dramatic",
) -> list[AdaptedSection]:
    """把清洗后的 dict 列表落地为 AdaptedSection，计算 source_shot_indices + section_id。

    combined_sections: list[(global_idx, Section, manifest_idx)] —— 多样例时合并后的参考池。
    跨样例（同一段引用了不同 manifest 的 sections）时 source_shot_indices 置空，因为
    shot 编号在不同 manifest 间会重号，无法稳定映射到 thumbnail。
    纯新增段（source_section_indices=[]）借用相邻段的 shots，让前端缩略图能展示。

    stage-24：把 LLM 给的 shots[] 归一化到 section 总时长后写入 AdaptedSection.shots。
    LLM 没给 shots[] 时不在这里兜底——交给 plan.py 在物化 Scene 时按需处理（也会在
    decompose 端复制 ShotPlan）。
    """
    if not items:
        return []

    out: list[AdaptedSection] = []
    n_src = len(combined_sections)
    last_shots: list[int] = []

    for order, it in enumerate(items):
        src_idx = [i for i in it.get("source_section_indices", []) if 0 <= i < n_src]
        # 检查 source 是否跨样例
        manifest_ids = {combined_sections[i][2] for i in src_idx}
        cross_sample = len(manifest_ids) > 1
        shot_indices: list[int] = []
        if not cross_sample:
            for i in src_idx:
                shot_indices.extend(combined_sections[i][1].shot_indices or [])
            seen = set()
            deduped: list[int] = []
            for s in shot_indices:
                if s not in seen:
                    seen.add(s)
                    deduped.append(s)
            shot_indices = deduped

        if not shot_indices and last_shots and not cross_sample:
            shot_indices = [last_shots[-1]]

        role = it["role"]
        section_dur = float(it.get("duration_seconds") or _default_duration_for(role, pattern))
        shots_list = _normalize_shot_durations(it.get("shots", []) or [], section_dur)
        out.append(AdaptedSection(
            section_id=f"sec-{order}",
            role=role,
            theme=it.get("theme", "") or _default_theme(role, pattern),
            content_description=it["content_description"],
            shots=shots_list,
            adaptation_note=it.get("adaptation_note", "") or "",
            tempo=it.get("tempo"),
            source_section_indices=src_idx,
            source_shot_indices=shot_indices,
            order=order,
            duration_seconds=section_dur,
        ))
        if shot_indices:
            last_shots = shot_indices

    return out


def _fallback_adaptation(sample_sections, target_total: float = 30.0, pattern: str = "dramatic") -> list[AdaptedSection]:
    """LLM 失败/为空时的兜底：1:1 拷贝样例段落，content_description 填占位，按 role 默认时长再缩放到目标总时长。"""
    out: list[AdaptedSection] = []
    n = len(sample_sections)
    if n == 0:
        return out
    raw_durs = [_default_duration_for(sec.role, pattern) for sec in sample_sections]
    total = sum(raw_durs) or 1.0
    scale = target_total / total if target_total > 0 else 1.0
    durs = [
        round(max(_MIN_SEC, min(_MAX_SEC, d * scale)), 1) for d in raw_durs
    ]
    for order, sec in enumerate(sample_sections):
        sec_dur = durs[order]
        out.append(AdaptedSection(
            section_id=f"sec-{order}",
            role=sec.role,
            theme=sec.theme or _default_theme(sec.role, pattern),
            content_description=(
                f"[fallback] 沿用样例 {sec.role} 段结构，"
                f"建议按本段镜头节奏组织画面与口播。"
            ),
            shots=_auto_shots_for_section(sec_dur, "", sec.role),
            adaptation_note="",
            tempo=None,
            source_section_indices=[order],
            source_shot_indices=list(sec.shot_indices or []),
            order=order,
            duration_seconds=sec_dur,
        ))
    return out


def _default_theme(role: str, pattern: str = "dramatic") -> str:
    """按 role + pattern 给默认中文短标签。step_N/item_N 取通用 '步骤 N'/'第 N 项'。"""
    import re as _re
    base: dict[str, str] = {
        # dramatic
        "opening": "开场钩子", "development": "主体铺陈", "climax": "卖点高潮", "closing": "行动引导",
        # stepwise
        "intro": "引入", "recap": "总结",
        # listicle
        "hook": "钩子", "closer": "收尾",
        # atmospheric
        "establish": "起势", "flow": "流转", "peak": "顶点", "resolve": "余韵",
        # info_dense
        "title_card": "标题卡", "info_block": "信息块", "payoff": "落版",
    }
    if role in base:
        return base[role]
    m = _re.match(r"^step_(\d+)$", role)
    if m:
        return f"步骤 {m.group(1)}"
    m = _re.match(r"^item_(\d+)$", role)
    if m:
        return f"第 {m.group(1)} 项"
    return role or "段落"
