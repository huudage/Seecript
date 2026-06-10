"""ShotPlan ↔ MaterialShot 匹配服务（stage-24 PR-B）。

为什么需要：plan_agent 给每个 AdaptedSection 拆出 1-3 个 ShotPlan（每个有
subject + visual 描述），用户上传的 Material 也已经被 PySceneDetect + VLM 切成
MaterialShot（每个有 caption + recommended_role + action_density）。但二者之间
没有对齐——plan.py 多镜头物化时只能按 `shot_idx % len(material.shots)` 循环取，
开场镜可能塞进收尾段。

本服务做"轻量文本相似度匹配"——不调 LLM，不依赖 embedding 服务（PR-B 先求快），
只用以下信号：
1. role 命中（material_shot.recommended_role == section_role）→ 强加权
2. subject/visual 与 caption 的中文双字 N-gram Jaccard
3. action_density 与 role 偏好的距离
4. 时长接近度（material_shot.duration ≈ shot.duration_seconds）

返回的匹配是"软指导"：plan.py 在物化时优先按匹配结果挑 MaterialShot，并把
in_point/out_point 切到该 MaterialShot 的 [start, end] 区间内。匹配失败（无 caption /
全部得分≈0）时回落到原 cyclic 策略。

PR-B 不引入嵌入向量；后续若效果不够好，可在本文件加 `_embed_match` 兼容路径。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from ...schemas import AdaptedSection, Material, MaterialShot, SectionRole, ShotPlan

log = logging.getLogger("seecript.agent.shot_matcher")


# 与 routers/plan.py:_ROLE_ACTION_PREFERENCE 保持同步——同一份偏好两边读。
_ROLE_ACTION_PREFERENCE: dict[str, float] = {
    "hook": 0.85, "opening": 0.75, "climax": 0.85, "transition_break": 0.7,
    "cta": 0.6, "closing": 0.25, "outro": 0.2, "ending": 0.2, "callback": 0.4,
    "summary": 0.3, "development": 0.5, "problem": 0.55, "twist": 0.8,
    "demonstration": 0.6, "tension": 0.75, "reveal": 0.85,
}


def _bigrams(text: str) -> set[str]:
    """中文双字 + 英文 token 双字 N-gram。粗暴但稳定，不依赖 jieba。"""
    if not text:
        return set()
    s = re.sub(r"[\s,，。.!！?？;；:：()（）\[\]【】\"'\\/<>]+", "", text.lower())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


@dataclass
class ShotMatch:
    """单个 ShotPlan 的匹配结果。"""

    shot_order: int
    material_id: Optional[str]
    material_shot_index: Optional[int]
    score: float  # 0-1，越高越好

    @property
    def matched(self) -> bool:
        return self.material_id is not None and self.material_shot_index is not None

    @property
    def quality(self) -> str:
        """stage-26 PR-N.2 三档分级：good≥0.30，weak≥0.10，<0.10=missing。

        物化层据此决策：missing 不再 cyclic 取错素材，改走 text_card 占位。
        阈值是经验值：0.10 = bigram Jaccard 几乎无重合且 role 也没匹配上，
        强行用对应素材必然跑题；0.30 = 中文双字 N-gram 命中 30% + role 加权，
        视觉一致性大概率成立。
        """
        if not self.matched:
            return "missing"
        if self.score >= 0.30:
            return "good"
        if self.score >= 0.10:
            return "weak"
        return "missing"


def _score_pair(
    plan_shot: ShotPlan, mat_shot: MaterialShot, section_role: SectionRole
) -> float:
    """一个 ShotPlan 配一个 MaterialShot 的匹配分（0-1）。

    分数构成（越高越好）：
    - text_jacc：subject+visual ↔ caption 双字 Jaccard，权重 0.55
    - role_match：recommended_role == section_role → +0.20
    - action_fit：action_density 与 role 偏好的接近度（1-|delta|），权重 0.15
    - dur_fit：时长接近度（1-|delta|/max），权重 0.10
    """
    plan_text = " ".join(s for s in (plan_shot.subject, plan_shot.visual) if s)
    cap = (mat_shot.caption or "").strip()
    text_jacc = _jaccard(_bigrams(plan_text), _bigrams(cap))

    role_match = 1.0 if (mat_shot.recommended_role or "").lower() == (section_role or "").lower() else 0.0

    pref = _ROLE_ACTION_PREFERENCE.get((section_role or "").lower(), 0.5)
    action_fit = 1.0 - min(1.0, abs((mat_shot.action_density or 0.5) - pref))

    target_dur = max(0.5, plan_shot.duration_seconds)
    dur_gap = abs(mat_shot.duration - target_dur) / max(target_dur, 0.5)
    dur_fit = max(0.0, 1.0 - min(1.0, dur_gap))

    return round(0.55 * text_jacc + 0.20 * role_match + 0.15 * action_fit + 0.10 * dur_fit, 4)


def match_section_shots(
    section: AdaptedSection,
    materials: list[Material],
    *,
    min_score: float = 0.05,
) -> list[ShotMatch]:
    """为 section.shots 中每个 ShotPlan 匹配一个最佳 MaterialShot。

    选材策略：
    - 在所有 materials 的所有 shots 池里挑全局最优（不限单个 material）
    - 一个 MaterialShot 不允许被同 section 内两个 ShotPlan 重复占用——挑完即占位
    - score < min_score 视为无匹配（matched=False）

    materials 顺序对结果有轻微偏好：scoring tie 时按 sort_order 在前者优先。

    本函数不修改 section / material；返回新 ShotMatch 列表，调用方决定怎么消费
    （写回 ShotPlan.matched_material_* 或直接在 plan.py 物化时按 score 排序选用）。

    stage-58：images 也参与匹配——为每个 image Material 合成一个虚拟 MaterialShot
    （caption=highlight_reason 或 subjects+tags 拼接，role=recommended_section，
    action_density=0.5 中性），与 video shots 同池竞争。
    """
    if not section.shots:
        return []
    pool: list[tuple[Material, MaterialShot]] = []
    for mat in materials:
        if mat.media_type == "video":
            if not mat.shots:
                continue
            for ms in mat.shots:
                pool.append((mat, ms))
        elif mat.media_type == "image":
            # 合成虚拟单 MaterialShot 让图片参与全局最优匹配
            cap = (mat.highlight_reason or "").strip()
            if not cap:
                # 拼 subjects + tags 当 caption（subjects 优先，标识具象主体）
                pieces: list[str] = []
                if mat.subjects:
                    pieces.extend(s for s in mat.subjects if s)
                if mat.tags:
                    pieces.extend(t for t in mat.tags[:4] if t)
                cap = " ".join(pieces).strip() or mat.filename or ""
            virt_dur = max(1.0, float(mat.duration_seconds or 3.0))
            virt_shot = MaterialShot(
                index=0,
                start=0.0,
                end=virt_dur,
                duration=virt_dur,
                caption=cap or None,
                action_density=0.5,
                recommended_role=mat.recommended_section,
            )
            pool.append((mat, virt_shot))
        # audio 类不参与
    if not pool:
        return [
            ShotMatch(shot_order=sh.order, material_id=None, material_shot_index=None, score=0.0)
            for sh in section.shots
        ]

    used: set[tuple[str, int]] = set()
    out: list[ShotMatch] = []
    for plan_shot in section.shots:
        best: Optional[tuple[float, Material, MaterialShot]] = None
        for mat, ms in pool:
            if (mat.material_id, ms.index) in used:
                continue
            score = _score_pair(plan_shot, ms, section.role)
            if best is None or score > best[0]:
                best = (score, mat, ms)
        if best is None or best[0] < min_score:
            out.append(ShotMatch(shot_order=plan_shot.order, material_id=None,
                                 material_shot_index=None, score=0.0))
            continue
        score, mat, ms = best
        used.add((mat.material_id, ms.index))
        out.append(ShotMatch(
            shot_order=plan_shot.order,
            material_id=mat.material_id,
            material_shot_index=ms.index,
            score=score,
        ))
    log.info(
        "[shot_matcher] section=%s role=%s shots=%d pool=%d matched=%d (avg score=%.3f)",
        section.section_id, section.role, len(section.shots), len(pool),
        sum(1 for m in out if m.matched),
        (sum(m.score for m in out if m.matched) / max(1, sum(1 for m in out if m.matched))),
    )
    return out


def apply_matches_to_section(section: AdaptedSection, matches: list[ShotMatch]) -> AdaptedSection:
    """把 ShotMatch 写回 section.shots[*].matched_material_id/shot_index + match_quality/score。

    stage-26 PR-N.2：除了原本写回 matched_material_*，再把 quality 三档 + 原始分数
    一并落到 ShotPlan，供 plan.py 物化层（missing → text_card 兜底）和前端段卡质量
    色条共用。

    返回新的 AdaptedSection（深拷贝）；原对象不变。matches 里 matched=False 的项
    保持 matched_material_* 为 None，match_quality 写 missing、score 写 0.0。
    """
    if not matches:
        return section
    by_order = {m.shot_order: m for m in matches}
    new_shots: list[ShotPlan] = []
    for sh in section.shots:
        m = by_order.get(sh.order)
        if m and m.matched:
            new_shots.append(sh.model_copy(update={
                "matched_material_id": m.material_id,
                "matched_material_shot_index": m.material_shot_index,
                "source_hint": sh.source_hint or "user_material",
                "match_quality": m.quality,
                "match_score": round(m.score, 4),
            }))
        elif m:
            # 未匹配也写 quality=missing + score=0，让前端能区分『从未跑过匹配』与『跑过但没匹上』
            new_shots.append(sh.model_copy(update={
                "match_quality": "missing",
                "match_score": 0.0,
            }))
        else:
            new_shots.append(sh)
    return section.model_copy(update={"shots": new_shots})
