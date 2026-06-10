"""测 detected_subjects → outline.content 的双保险机制（#420 / stage-31 兜底）：

LLM 经常会忽视「必须把 detected_subjects 写进 content」的提示——所以在 yield 前
机械地把缺的对象拼到 content 末尾。这层兜底保证用户上传纸巾，content 就一定有「纸巾」。
"""

from __future__ import annotations

from app.schemas import ClarifyOutline
from app.services.agent.clarify_agent import _enforce_subjects_in_content


def test_subjects_appended_when_llm_misses_them() -> None:
    o = ClarifyOutline(
        topic="家清测评",
        content="评测家用清洁套装，对比实测",
        audience="家庭主妇",
        goal="种草",
        tone="温暖",
    )
    out = _enforce_subjects_in_content(o, ["纸巾", "海绵"])
    assert out.content is not None
    assert "纸巾" in out.content
    assert "海绵" in out.content
    # 其他字段不动
    assert out.topic == "家清测评"
    assert out.audience == "家庭主妇"


def test_subjects_already_present_is_noop() -> None:
    o = ClarifyOutline(topic="t", content="纸巾真的好用", audience=None, goal=None, tone=None)
    out = _enforce_subjects_in_content(o, ["纸巾"])
    assert out.content == "纸巾真的好用"


def test_partial_missing_subjects_only_appended_for_missing() -> None:
    o = ClarifyOutline(
        topic="t", content="测评纸巾包装效果", audience=None, goal=None, tone=None
    )
    out = _enforce_subjects_in_content(o, ["纸巾", "海绵"])
    assert out.content is not None
    # 「纸巾」已在 → 不重复；「海绵」缺 → 补
    assert out.content.count("纸巾") == 1
    assert "海绵" in out.content


def test_empty_content_uses_fallback_template() -> None:
    o = ClarifyOutline(topic="t", content=None, audience=None, goal=None, tone=None)
    out = _enforce_subjects_in_content(o, ["纸巾", "海绵"])
    assert out.content is not None
    assert "纸巾" in out.content and "海绵" in out.content


def test_empty_subjects_is_noop() -> None:
    o = ClarifyOutline(topic="t", content="abc", audience=None, goal=None, tone=None)
    out = _enforce_subjects_in_content(o, [])
    assert out.content == "abc"


def test_whitespace_only_subjects_filtered() -> None:
    o = ClarifyOutline(topic="t", content="abc", audience=None, goal=None, tone=None)
    out = _enforce_subjects_in_content(o, ["", "  ", "\t"])
    assert out.content == "abc"


def test_clamped_to_200_chars() -> None:
    long_content = "这是一段很长的内容描述" * 25  # ~225 chars
    o = ClarifyOutline(topic="t", content=long_content, audience=None, goal=None, tone=None)
    out = _enforce_subjects_in_content(o, ["纸巾"])
    assert out.content is not None
    assert len(out.content) <= 200


# ---- 意图清洗：detected_subjects → (relevant, dropped) ----
# 用户上传带货模特图，VLM 同时识别出「干脆面饼」「耳钉」「项链」「美甲」「近景特写」。
# 意图是带货干脆面，所以耳钉/项链/美甲/构图词全是陪衬，必须被剔除——否则下游
# plan_agent 会把耳钉拉成单独一镜头（用户报告的 bug）。

from app.services.agent.clarify_agent import _coerce_relevant_detected_subjects


def test_coerce_drops_accessory_hints_even_if_llm_keeps_them() -> None:
    detected = ["干脆面饼", "耳钉", "项链", "美甲", "客厅"]
    # LLM 误把耳钉当主体
    relevant_raw = ["干脆面饼", "耳钉", "客厅"]
    relevant, dropped = _coerce_relevant_detected_subjects(relevant_raw, detected)
    assert "干脆面饼" in relevant
    assert "客厅" in relevant
    # 强制黑名单：accessory hints 一律剔除
    assert "耳钉" not in relevant
    assert "耳钉" in dropped
    # 项链/美甲虽然 LLM 没标，也应在 dropped（因为不在 relevant 里）
    assert "项链" in dropped
    assert "美甲" in dropped


def test_coerce_drops_meta_blacklist() -> None:
    detected = ["干脆面饼", "近景特写", "暖调光线", "美食种草"]
    relevant_raw = ["干脆面饼", "近景特写", "暖调光线", "美食种草"]
    relevant, dropped = _coerce_relevant_detected_subjects(relevant_raw, detected)
    assert relevant == ["干脆面饼"]
    assert "近景特写" in dropped and "暖调光线" in dropped and "美食种草" in dropped


def test_coerce_rejects_llm_fabricated_items() -> None:
    """LLM 不能编造 detected 里没有的项；编造的丢弃。"""
    detected = ["纸巾", "客厅"]
    relevant_raw = ["纸巾", "马桶"]  # 「马桶」是 LLM 凭空脑补的
    relevant, dropped = _coerce_relevant_detected_subjects(relevant_raw, detected)
    assert "纸巾" in relevant
    assert "马桶" not in relevant
    # dropped = detected - relevant；马桶不在 detected 所以不在 dropped
    assert dropped == ["客厅"]


def test_coerce_empty_detected_returns_empty() -> None:
    assert _coerce_relevant_detected_subjects(["a"], []) == ([], [])
    assert _coerce_relevant_detected_subjects(["a"], None) == ([], [])


def test_coerce_handles_string_and_list_input() -> None:
    detected = ["纸巾", "海绵"]
    # 字符串顿号串
    rel1, drop1 = _coerce_relevant_detected_subjects("纸巾、海绵", detected)
    assert set(rel1) == {"纸巾", "海绵"}
    assert drop1 == []
    # list
    rel2, drop2 = _coerce_relevant_detected_subjects(["纸巾"], detected)
    assert rel2 == ["纸巾"]
    assert drop2 == ["海绵"]


def test_coerce_dedup_preserves_order() -> None:
    detected = ["A", "B", "C"]
    rel, drop = _coerce_relevant_detected_subjects(["B", "B", "A"], detected)
    assert rel == ["B", "A"]  # 保留 LLM 给的顺序，去重
    assert drop == ["C"]
