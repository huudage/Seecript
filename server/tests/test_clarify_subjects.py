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
