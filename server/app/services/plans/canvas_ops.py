"""v2 结构画布的即时结构操作（PRD-v2 F3/F4 · Epic-3 D2）。

与 compose_edit_agent 的 NL 编辑 mutator 分流：那边走 LLM 工具调用链 + diff 摘要；
这边是人类画布直接操作（段落块拖拽重排 / 实拍块切分），F13 契约——即时生效，
前端 editStore 撤销栈兜底，服务端只做结构落盘，不跑任何 LLM。

分组口径与前端 StoryboardCanvas / FourTrackBoard 一致：
Scene.parent_section_id 优先；老 plan 无此字段时按 scene_id `sc-N` 正则对
AdaptedSection.order 兜底。绝不能用 scene.section（role）分组——v2 F3 允许多段同 role，
compose_edit_agent._rebuild_timeline 的 role 分组在 v2 数据上会把同 role 段搅在一起。
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

from ...schemas import AdaptedSection, Plan, Scene

_SC_RE = re.compile(r"sc-(\d+)")

# US-3.5：只有实拍源可切分（用户素材 / 样例镜头）
_REAL_SOURCES = {"user_material", "sample"}
# 拥有这些时间锚定轴的块不可切分——切分会破坏内层与块时间轴的对齐
_SPLIT_AXIS_KINDS = {"subtitle", "title_bar"}
# AdaptedSection.duration_seconds 的 pydantic 硬约束 ge=2.0——切出的两段各须达标
_MIN_SECTION_SECONDS = 2.0
_MIN_SHOT_SECONDS = 0.5


def _scene_section_id(sc: Scene, order_to_sid: dict[int, str]) -> Optional[str]:
    if sc.parent_section_id:
        return sc.parent_section_id
    m = _SC_RE.match(sc.scene_id)
    if m:
        return order_to_sid.get(int(m.group(1)))
    return None


def group_scenes_by_section(plan: Plan) -> tuple[dict[str, list[Scene]], list[Scene]]:
    """按段分组 scenes（段内按 start/shot_order 排序）。

    返回 (groups, orphans)：orphans 是解析不出所属段的 scene（重排时保持原相对顺序追加到主轨末尾，
    与 compose_edit_agent._rebuild_timeline 的老数据兼容行为一致）。
    """
    order_to_sid = {sec.order: sec.section_id for sec in plan.adapted_sections}
    groups: dict[str, list[Scene]] = {sec.section_id: [] for sec in plan.adapted_sections}
    orphans: list[Scene] = []
    for sc in plan.main_track:
        sid = _scene_section_id(sc, order_to_sid)
        if sid in groups:
            groups[sid].append(sc)
        else:
            orphans.append(sc)
    for lst in groups.values():
        lst.sort(key=lambda s: (s.start, s.shot_order))
    return groups, orphans


def _materialize_parent_ids(plan: Plan) -> None:
    """把 `sc-N` 正则兜底解析出的归属写死进 parent_section_id。

    重排/切分都会重写 AdaptedSection.order（位移），此后正则按「新 order」解析老 scene_id
    必然错位——所以任何 order 位移操作前先物化一次，之后全链路只认 parent_section_id。
    """
    order_to_sid = {sec.order: sec.section_id for sec in plan.adapted_sections}
    for sc in plan.main_track:
        if not sc.parent_section_id:
            sid = _scene_section_id(sc, order_to_sid)
            if sid:
                sc.parent_section_id = sid


def _relay_timeline(
    plan: Plan,
    ordered_groups: list[tuple[AdaptedSection, list[Scene]]],
    orphans: list[Scene],
) -> dict:
    """按段顺序重铺主轨：重算 start、清字幕、裁超界包装、回写总时长。

    语义与 compose_edit_agent._rebuild_timeline 对齐（字幕一律清空请用户回 step3 重生成、
    其它包装项超出新总长的截断或丢弃、duration_seconds 跟随 main_track 总和伸缩），
    唯一差别是分组维度从 role 换成 parent_section_id。
    """
    new_track: list[Scene] = []
    t = 0.0
    moved = 0
    for _sec, scenes in ordered_groups:
        for sc in scenes:
            if abs(sc.start - t) > 0.01:
                moved += 1
            sc.start = round(t, 3)
            t += sc.duration
            new_track.append(sc)
    for sc in orphans:
        sc.start = round(t, 3)
        t += sc.duration
        new_track.append(sc)
    plan.main_track = new_track
    total = round(t, 3)

    subtitles_cleared = 0
    trimmed = 0
    new_pkg = []
    for it in plan.packaging_track:
        if it.kind == "subtitle":
            subtitles_cleared += 1
            continue
        if it.start >= total + 0.01:
            trimmed += 1
            continue
        if it.end > total + 0.01:
            it.end = total
            trimmed += 1
        new_pkg.append(it)
    plan.packaging_track = new_pkg

    if plan.settings is not None and total > 0:
        plan.settings.target_duration_seconds = max(10.0, min(300.0, total))
    plan.duration_seconds = total
    return {
        "scenes_moved": moved,
        "subtitles_cleared": subtitles_cleared,
        "packaging_trimmed": trimmed,
        "total": total,
    }


def reorder_sections(plan: Plan, section_ids: list[str]) -> dict:
    """画布段落块拖拽重排（F4/US-3.2）：就地改写 plan。

    section_ids 必须是当前全部段落 id 的一个排列；顺序即新的叙事链序。
    raises ValueError：排列校验失败时（路由层翻译成 422）。
    """
    existing = [s.section_id for s in plan.adapted_sections]
    if len(section_ids) != len(existing) or set(section_ids) != set(existing):
        raise ValueError("section_ids 必须是当前全部段落 id 的重排（不多不少）")
    if section_ids == existing:
        return {"reordered": False, "section_ids": existing}

    _materialize_parent_ids(plan)
    groups, orphans = group_scenes_by_section(plan)

    id_to_sec = {s.section_id: s for s in plan.adapted_sections}
    new_secs = [id_to_sec[i] for i in section_ids]
    for i, s in enumerate(new_secs):
        s.order = i
    plan.adapted_sections = new_secs

    info = _relay_timeline(plan, [(s, groups.get(s.section_id, [])) for s in new_secs], orphans)
    info["reordered"] = True
    info["section_ids"] = section_ids
    return info


def _unique_id(base: str, taken: set[str]) -> str:
    """base 已被占用时追加随机后缀，直到唯一。"""
    if base not in taken:
        return base
    return f"{base}-{uuid.uuid4().hex[:6]}"


def split_scene(plan: Plan, scene_id: str, split_at: float) -> dict:
    """实拍块时间轴切分（F4/US-3.5）：镜一分为二 + 段拆两段，就地改写 plan。

    split_at 是切点距本镜起点的秒数。边界规则（PRD Q3 拍板）：
    - 只有实拍源（user_material / sample）可切
    - 块内含字幕 / 标题条 / 口播音轨时不可切——须先摘除内层
    - 切点距镜两端 ≥ 0.5s；切出的两段各自总时长 ≥ 2s（duration_seconds 硬约束）

    raises ValueError：任何边界规则不满足时（路由层翻译成 422）。
    """
    scene = next((s for s in plan.main_track if s.scene_id == scene_id), None)
    if scene is None:
        raise ValueError(f"scene_id 不存在：{scene_id}")
    if scene.source not in _REAL_SOURCES:
        raise ValueError("只有实拍块（用户素材 / 样例镜头）可切分")

    _materialize_parent_ids(plan)
    groups, _orphans = group_scenes_by_section(plan)
    sid = scene.parent_section_id or ""
    sec = next((s for s in plan.adapted_sections if s.section_id == sid), None)
    if sec is None or not groups.get(sid):
        raise ValueError("该分镜不属于任何段落，无法切分")
    block_scenes = groups[sid]
    block_start = block_scenes[0].start
    block_end = block_scenes[-1].start + block_scenes[-1].duration

    axis_names = {"subtitle": "字幕", "title_bar": "标题条"}
    for it in plan.packaging_track:
        if it.kind in _SPLIT_AXIS_KINDS and it.start < block_end - 0.01 and it.end > block_start + 0.01:
            raise ValueError(
                f"本块含{axis_names[it.kind]}轴（{it.start:.1f}-{it.end:.1f}s），先摘除内层再切分"
            )
    if any(sc.voiceover_url for sc in block_scenes):
        raise ValueError("本块含口播音轨，先摘除口播再切分")

    if not (_MIN_SHOT_SECONDS <= split_at <= scene.duration - _MIN_SHOT_SECONDS):
        raise ValueError(
            f"切点须距镜两端 ≥ {_MIN_SHOT_SECONDS}s（当前 {split_at:.1f} / 镜长 {scene.duration:.1f}s）"
        )

    pos = block_scenes.index(scene)
    first_dur = round(sum(s.duration for s in block_scenes[:pos]) + split_at, 3)
    second_dur = round(
        (scene.duration - split_at) + sum(s.duration for s in block_scenes[pos + 1:]), 3
    )
    if first_dur < _MIN_SECTION_SECONDS or second_dur < _MIN_SECTION_SECONDS:
        raise ValueError(
            f"切分后两段各需 ≥ {_MIN_SECTION_SECONDS:.0f}s（当前 {first_dur:.1f} / {second_dur:.1f}s）"
        )

    # 镜一分为二：前半保留原 scene_id（老正则兜底继续命中），后半拿新 id 并显式挂 parent
    new_scene_id = _unique_id(f"{scene_id}-b", {s.scene_id for s in plan.main_track})
    scene_a = scene.model_copy(update={
        "duration": round(split_at, 3),
        "out_point": scene.in_point + split_at,
    })
    scene_b = scene.model_copy(update={
        "scene_id": new_scene_id,
        "duration": round(scene.duration - split_at, 3),
        "in_point": scene.in_point + split_at,
        "start": scene.start + split_at,
        "shot_order": 0,
        "narration": None,
        "user_edited": False,
    })

    # 段拆两段：B 段 shots 从切点 shot 起复制并重排 order，B 段各镜 shot_order 同步减 k
    new_sec_id = _unique_id(f"{sid}-b", {s.section_id for s in plan.adapted_sections})
    k = scene.shot_order
    shots = sec.shots or []
    sec_a = sec.model_copy(update={
        "shots": shots[: k + 1],
        "duration_seconds": first_dur,
    })
    sec_b = sec.model_copy(update={
        "section_id": new_sec_id,
        "shots": [sh.model_copy(update={"order": i}) for i, sh in enumerate(shots[k:])],
        "duration_seconds": second_dur,
        "order": sec.order + 1,
        "source_section_indices": [],
    })

    scene_b.parent_section_id = new_sec_id
    b_scenes: list[Scene] = [scene_b]
    for sc in block_scenes[pos + 1:]:
        sc.parent_section_id = new_sec_id
        sc.shot_order = max(0, sc.shot_order - k)
        b_scenes.append(sc)

    # 主轨：切点镜替换为两半；其余镜绝对时间都不动（总时长不变），无需重铺
    scene_idx = plan.main_track.index(scene)
    plan.main_track[scene_idx:scene_idx + 1] = [scene_a, scene_b]

    # 段列表：原位替换为两段；后续段 order 顺次 +1
    sec_idx = next(i for i, s in enumerate(plan.adapted_sections) if s.section_id == sid)
    plan.adapted_sections[sec_idx:sec_idx + 1] = [sec_a, sec_b]
    for s in plan.adapted_sections[sec_idx + 2:]:
        s.order += 1

    return {
        "scene_a": scene_id,
        "scene_b": new_scene_id,
        "section_a": sid,
        "section_b": new_sec_id,
        "first_duration": first_dur,
        "second_duration": second_dur,
    }


def append_section(plan: Plan, sec: AdaptedSection, scene: Scene) -> dict:
    """画布空白处添加视频块（F6 结构动作 / US-4）：新段追加到叙事链末尾。

    sec.shots 与 scene 的素材内容（含 Seedream / Seedance / TextCardSpec 物化结果）
    由路由层准备好传入；本函数只做结构落位——即时生效（F13），不跑 LLM。
    段内首镜 shot_order=0 并挂 parent_section_id；_relay_timeline 统一重铺 start
    并钳全片时长。
    """
    _materialize_parent_ids(plan)

    if sec.duration_seconds < _MIN_SECTION_SECONDS:
        raise ValueError(f"新段落块需 ≥ {_MIN_SECTION_SECONDS:.0f}s（当前 {sec.duration_seconds:.1f}s）")
    if any(s.section_id == sec.section_id for s in plan.adapted_sections):
        raise ValueError(f"section_id 已存在：{sec.section_id}")
    if any(s.scene_id == scene.scene_id for s in plan.main_track):
        raise ValueError(f"scene_id 已存在：{scene.scene_id}")

    sec.order = max((s.order for s in plan.adapted_sections), default=-1) + 1
    scene.parent_section_id = sec.section_id
    scene.shot_order = 0
    scene.start = 0.0  # 占位，relay 统一重铺

    plan.adapted_sections.append(sec)
    plan.main_track.append(scene)

    groups, orphans = group_scenes_by_section(plan)
    info = _relay_timeline(plan, [(s, groups.get(s.section_id, [])) for s in plan.adapted_sections], orphans)
    return {
        "section_id": sec.section_id,
        "scene_id": scene.scene_id,
        "order": sec.order,
        "total": info["total"],
    }
