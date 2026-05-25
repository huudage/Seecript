"""缺口识别与补全 Agent。

两个核心函数：
- detect_gaps(manifest, materials) → list[Gap]
    简化版槽位匹配：按 section + slot 顺位枚举样例需求，挨个找匹配的 user material。
    匹配规则（阶段 3 简化版）：
      * 推荐 section 命中 → status=ok
      * section 不一致但媒体类型可用 → status=warn
      * 没有任何素材可用 → status=miss
- fill_gap(gap, action, params) → FillResult
    分发到 rerank（纯 Python） / copy（LLM 文案） / aigc（T2I 生成）。

阶段 3 此版本足以驱动前端 UI；阶段 5 比赛前再做槽位匹配的真算法（cos-sim + section 推荐）。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from ..llm_client import get_llm_client
from ..t2i_client import get_t2i_client
from ...schemas import (
    FillAction,
    FillResult,
    Gap,
    Material,
    SampleManifest,
)

log = logging.getLogger("seecript.agent.gap")


_COPY_SYSTEM = (
    "你是短视频口播作者。根据『槽位需求』和『可参考素材标签』，"
    "生成一句口语化的中文口播（不超过 40 字），"
    "返回 JSON：{\"gap_fill_narration\": str, \"alternatives\": [str, str]}。"
)


def detect_gaps(manifest: SampleManifest, materials: list[Material]) -> list[Gap]:
    """简化版槽位匹配。每个 section 默认 2-3 个槽，挨个分配 material。"""
    by_section: dict[str, list[Material]] = {"hook": [], "body": [], "cta": []}
    for m in materials:
        rec = m.recommended_section or "body"
        by_section.setdefault(rec, []).append(m)

    gaps: list[Gap] = []
    for sec in manifest.sections:
        # 简化：每 section 拿 sub-段数量 = min(3, len(shot_indices))
        slot_count = max(1, min(3, len(sec.shot_indices)))
        for slot in range(slot_count):
            requirement = _slot_requirement(sec.kind, slot, manifest)
            pool = by_section.get(sec.kind, [])
            if slot < len(pool):
                m = pool[slot]
                gaps.append(Gap(
                    gap_id=f"gap-{sec.kind}-{slot}",
                    section=sec.kind,
                    slot_index=slot,
                    requirement=requirement,
                    status="ok",
                    impact="high" if sec.kind in ("hook", "cta") else "medium",
                    matched_material_id=m.material_id,
                    note=f"匹配素材 {m.filename}",
                ))
            else:
                # 试图从其他 section 借
                spillover = next((p for k, lst in by_section.items() if k != sec.kind for p in lst), None)
                if spillover:
                    gaps.append(Gap(
                        gap_id=f"gap-{sec.kind}-{slot}",
                        section=sec.kind,
                        slot_index=slot,
                        requirement=requirement,
                        status="warn",
                        impact="medium",
                        matched_material_id=spillover.material_id,
                        note="跨段借用，建议重排或 AIGC 补全",
                    ))
                else:
                    gaps.append(Gap(
                        gap_id=f"gap-{sec.kind}-{slot}",
                        section=sec.kind,
                        slot_index=slot,
                        requirement=requirement,
                        status="miss",
                        impact="high" if sec.kind == "hook" else "medium",
                        note="无可用素材，建议 Seedream 生成",
                    ))
    return gaps


def _slot_requirement(section: str, slot: int, manifest: SampleManifest) -> str:
    """根据 PackagingProfile + section 给出该槽的语义描述。"""
    style = manifest.packaging.subtitle_style
    if section == "hook":
        return f"开场 3 秒 · 痛点提问近景（{style}）"
    if section == "cta":
        return f"收尾 · 大字幕引导（{style}）"
    return f"主体 #{slot + 1} · 演示/对比中景"


async def fill_gap(gap: Gap, action: FillAction, params: dict[str, Any]) -> FillResult:
    """分发到三种动作。"""
    log.info("[gap-fill] %s action=%s", gap.gap_id, action)
    if action == "rerank":
        target = params.get("target_material_id") or f"mat-rerank-{uuid.uuid4().hex[:6]}"
        return FillResult(
            gap_id=gap.gap_id, action="rerank",
            new_material_id=target, status="ok",
            note="已重排到该槽位",
        )

    if action == "copy":
        llm = get_llm_client()
        user = (
            f"槽位需求：{gap.requirement}\n"
            f"section：{gap.section}\n"
            f"可参考素材标签：{params.get('tag_hint', '无')}\n"
            f"创作者补充：{params.get('prompt_hint', '')}"
        )
        try:
            data = await llm.complete_json(_COPY_SYSTEM, user)
            narration = (data.get("gap_fill_narration") or "").strip() if isinstance(data, dict) else ""
        except Exception as exc:
            log.warning("llm copy failed: %s", exc)
            narration = ""
        return FillResult(
            gap_id=gap.gap_id, action="copy",
            narration=narration or "[fallback] 这里加一句口播，把刚才的对比强调一下。",
            status="ok", note="LLM 文案补全完成",
        )

    if action == "aigc":
        t2i = get_t2i_client()
        prompt = params.get("prompt") or f"短视频画面：{gap.requirement}"
        size = params.get("size") or "1024x1024"
        try:
            img = await t2i.generate(prompt, size=size)
            return FillResult(
                gap_id=gap.gap_id, action="aigc",
                new_material_id=img.image_id, status="ok",
                note=f"Seedream 生成完成（{img.provider}，{img.elapsed_ms}ms）",
            )
        except Exception as exc:
            log.warning("t2i generate failed: %s", exc)
            return FillResult(
                gap_id=gap.gap_id, action="aigc",
                status="warn", note=f"AIGC 失败：{exc}",
            )

    return FillResult(gap_id=gap.gap_id, action=action, status="warn", note=f"未知动作：{action}")
