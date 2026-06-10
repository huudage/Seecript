"""Material × Section 适配度打分（stage-59）。

设计动机：用户只能人工挑素材换段（stage-59 把 3 档自动策略下线了），那 UI 必须告诉他
"这条素材跟这段有多搭"，否则人工选完全靠瞎猜——尤其素材库 50+ 条的项目。

打分输入：
  - section: AdaptedSection（段位 role + theme + content_description + duration_seconds）
  - material: Material（recommended_section + tags + highlight_score + duration_seconds）
  - scene_duration: 本镜目标时长（不一定等于 section 整体时长，AdaptedSection 拆 N 镜时各镜各自有）

打分维度（权重和必须 = 1.0；分数最终 clamp 到 [0,1]）：
  - 段位推荐 0.40：material.recommended_section == section.role  → 1.0；其它 → 0
  - 主体匹配 0.30：material.tags ∩ section 关键词 命中比例（命中一个 +0.5、两个+ +1.0；上限 1.0）
  - 时长贴合 0.20：1 - clamp(|scene_duration - material.duration| / max(scene_duration, 1.0), 0, 1)
  - 高光评分 0.10：material.highlight_score（已 0-1，缺省按 0.4 给）

reason 拼一句话（≤80 字），描述命中的强信号；按重要度排前面。

使用方：
  - plan_agent.build_plan：组完 main_track 后批量调一次，给每个 user_material scene 写 fit_score
  - gap_agent._fill_rerank：换源后给新 scene 重算
  - routers/plan.py 加一个 /plan/{plan_id}/refresh-fit-scores 路由（用户在 UI 点"刷新评分"）
  - routers/scene.py 切素材时也在尾部调
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from ...schemas import AdaptedSection, Material, Scene

log = logging.getLogger("seecript.materials.fit")

_W_SECTION = 0.40
_W_TAG = 0.30
_W_DURATION = 0.20
_W_HIGHLIGHT = 0.10

# 中文 + 英文/数字 token 切分（剔除单字虚词），用于 section 关键词与 material.tags 的交集判定
_TOKEN_RE = re.compile(r"[一-鿿]{2,}|[A-Za-z][A-Za-z0-9]+")
_STOPWORDS = {
    "的", "了", "和", "与", "或", "在", "是", "有", "也", "就", "都", "把", "到",
    "不", "很", "上", "下", "中", "里", "this", "that", "the", "and", "with", "for",
}


def _tokenize_section(section: AdaptedSection) -> set[str]:
    """段落关键词池：theme + content_description 切 2+ 字 token。"""
    text = f"{section.theme or ''} {section.content_description or ''}"
    tokens = {m.group(0).lower() for m in _TOKEN_RE.finditer(text)}
    return {t for t in tokens if t not in _STOPWORDS}


def _tag_overlap_ratio(material: Material, section_tokens: set[str]) -> tuple[float, list[str]]:
    """命中比例 + 命中的具体 tag 列表（用于 reason）。

    单 tag 命中即 0.5；两个+ 命中拉满 1.0。设计上鼓励有任何重合即认定可用，
    避免纯长尾词搜索影响分数稳定性。
    """
    tags = [t for t in (material.tags or []) if t]
    if not tags:
        # 没有 tags 但 material.subject_anchor 也算一种主体描述
        sub = (getattr(material, "subject_anchor", None) or "").strip().lower()
        if sub and any(tok in sub or sub in tok for tok in section_tokens):
            return 0.5, [sub]
        return 0.0, []
    hits: list[str] = []
    for t in tags:
        tl = t.strip().lower()
        if not tl:
            continue
        # 任一 token 子串包含或反向包含都算命中（中文不分词，"奶茶" / "新品奶茶" 互算）
        for tok in section_tokens:
            if tok and (tok in tl or tl in tok):
                hits.append(t)
                break
    if not hits:
        return 0.0, []
    if len(hits) == 1:
        return 0.5, hits
    return 1.0, hits[:3]


def _duration_match(scene_duration: float, material_duration: Optional[float]) -> float:
    """0..1。素材时长缺失（图片）按 0.5 中性给——不奖也不罚。"""
    if not material_duration or material_duration <= 0:
        return 0.5
    if scene_duration <= 0:
        return 0.5
    diff = abs(scene_duration - material_duration)
    base = max(scene_duration, 1.0)
    raw = 1.0 - min(diff / base, 1.0)
    return max(0.0, min(1.0, raw))


def _build_reason(
    *,
    section_hit: bool,
    tag_hits: list[str],
    duration_score: float,
    highlight: float,
) -> str:
    """按强信号优先排：段位 > 主体 > 时长 > 高光。"""
    parts: list[str] = []
    if section_hit:
        parts.append("段位推荐命中")
    if tag_hits:
        parts.append(f"主体匹配（{','.join(tag_hits[:2])}）")
    if duration_score >= 0.8:
        parts.append("时长贴合")
    elif duration_score <= 0.3:
        parts.append("时长偏离")
    if highlight >= 0.7:
        parts.append(f"高光 {highlight:.2f}")
    if not parts:
        parts.append("整体偏离段意，可考虑换一条")
    return " · ".join(parts)[:80]


def compute_material_fit(
    *,
    material: Material,
    section: AdaptedSection,
    scene_duration: float,
) -> tuple[float, str]:
    """返回 (fit_score 0-1, fit_reason ≤80 字)。"""
    section_hit = (material.recommended_section == section.role)
    section_score = 1.0 if section_hit else 0.0

    tokens = _tokenize_section(section)
    tag_score, tag_hits = _tag_overlap_ratio(material, tokens)

    duration_score = _duration_match(scene_duration, material.duration_seconds)

    highlight = float(material.highlight_score or 0.4)
    highlight = max(0.0, min(1.0, highlight))

    raw = (
        _W_SECTION * section_score
        + _W_TAG * tag_score
        + _W_DURATION * duration_score
        + _W_HIGHLIGHT * highlight
    )
    score = max(0.0, min(1.0, raw))
    reason = _build_reason(
        section_hit=section_hit,
        tag_hits=tag_hits,
        duration_score=duration_score,
        highlight=highlight,
    )
    return score, reason


def annotate_plan_fit_scores(
    *,
    main_track: list[Scene],
    adapted_sections: list[AdaptedSection],
    materials_by_id: dict[str, Material],
) -> int:
    """批量给 plan.main_track 上的 user_material scene 写 fit_score / fit_reason。

    返回写入的 scene 数量（其它 source 类型 / 找不到素材的跳过，且会清空残留 fit_score
    避免老 plan 切换 source 后还挂着上一个 material 的分）。
    """
    sec_by_id = {s.section_id: s for s in adapted_sections}
    sec_by_role: dict[str, AdaptedSection] = {}
    for s in adapted_sections:
        sec_by_role.setdefault(s.role, s)

    written = 0
    for scene in main_track:
        if scene.source != "user_material":
            scene.fit_score = None
            scene.fit_reason = None
            continue
        material = materials_by_id.get(scene.source_ref)
        if not material:
            scene.fit_score = None
            scene.fit_reason = None
            continue
        # 优先用 parent_section_id；缺时按 scene.section role 取第一个
        section = sec_by_id.get(scene.parent_section_id or "")
        if not section:
            section = sec_by_role.get(scene.section)
        if not section:
            scene.fit_score = None
            scene.fit_reason = None
            continue
        score, reason = compute_material_fit(
            material=material, section=section, scene_duration=float(scene.duration or 0.0),
        )
        scene.fit_score = round(score, 3)
        scene.fit_reason = reason
        written += 1
    return written
