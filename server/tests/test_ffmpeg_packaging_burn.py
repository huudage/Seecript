"""ffmpeg drawtext fallback —— Remotion 不可用时，packaging_track 必须烧到主轨上。

不依赖系统 ffmpeg：用 monkeypatch 截 subprocess.run + ffmpeg_available，
只断言 filter_complex 字符串的构造是否包含期望文本/时间窗。

真正烧字幕的端到端验证由 test_e2e_pipeline 在装了 ffmpeg 的机器上覆盖。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.services.video import ffmpeg as ffmpeg_svc


@pytest.fixture
def fake_ffmpeg(monkeypatch, tmp_path):
    """让 ffmpeg_available() 返 True 并捕获 subprocess 调用，不实际跑 ffmpeg。"""
    captured: dict = {}

    def fake_run(cmd, capture_output=True, text=True, check=False):
        captured["cmd"] = cmd
        # 落一个非空文件让 caller 觉得渲染成功
        dst = Path(cmd[-1])
        dst.write_bytes(b"\x00" * 64)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(ffmpeg_svc, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(subprocess, "run", fake_run)
    return captured


def test_burn_packaging_contains_text_and_window(fake_ffmpeg, tmp_path):
    src = tmp_path / "main.mp4"
    src.write_bytes(b"\x00" * 128)
    dst = tmp_path / "overlaid.mp4"

    items = [
        {
            "item_id": "pkg-sub-0",
            "kind": "subtitle",
            "start": 0.0,
            "end": 3.5,
            "text": "差异化卖点，3 秒抓住你",
            "style": {},
        },
        {
            "item_id": "pkg-title",
            "kind": "title_bar",
            "start": 0.0,
            "end": 2.0,
            "text": "新品速览",
            "style": {},
        },
        {
            "item_id": "pkg-cta",
            "kind": "sticker",
            "start": 10.0,
            "end": 13.0,
            "text": "点击主页预约",
            "style": {},
        },
        {
            "item_id": "pkg-cover",
            "kind": "cover",
            "start": 0.0,
            "end": 1.2,
            "text": "封面标题",
            "style": {"subtitle": "副标题文字"},
        },
        {
            "item_id": "pkg-tr-0",
            "kind": "transition",
            "start": 5.0,
            "end": 5.4,
            "text": None,
            "style": {"transition_style": "dissolve"},
        },
    ]

    out = ffmpeg_svc.burn_packaging_track(src, items, dst)
    assert out == dst
    assert out.exists() and out.stat().st_size > 0

    vf = " ".join(fake_ffmpeg["cmd"])
    # 5 类元素都被构造进了滤镜
    assert "差异化卖点" in vf
    assert "新品速览" in vf
    assert "点击主页预约" in vf
    assert "封面标题" in vf
    assert "副标题文字" in vf  # cover 的 subtitle 也烧进去
    # 时间窗 between(t,start,end) 出现
    assert "between(t" in vf and "3.500" in vf and "13.000" in vf
    # 注意：转场已迁移到 Scene.transition_in（xfade 滤镜），
    # burn_packaging_track 收到 kind="transition" 时只 log warning + 跳过；
    # 这里断言它没有偷偷生成 drawbox/drawtext，但仍要把其它 4 类正常烧进去。
    assert "drawbox" not in vf
    # subtitle/title_bar/sticker/cover 都用 drawtext
    assert vf.count("drawtext") >= 5  # 4 类 text + 1 个 cover subtitle


def test_burn_packaging_no_items_passes_through(fake_ffmpeg, tmp_path):
    """空 packaging → 不调 ffmpeg，直接复制（fallback 兜底，pipeline 不挂）。"""
    src = tmp_path / "main.mp4"
    src.write_bytes(b"main-content")
    dst = tmp_path / "overlaid.mp4"

    out = ffmpeg_svc.burn_packaging_track(src, [], dst)
    assert out == dst
    assert out.read_bytes() == b"main-content"
    # 空 items → 不该调 subprocess
    assert "cmd" not in fake_ffmpeg


def test_burn_packaging_skips_invalid_window(fake_ffmpeg, tmp_path):
    """end <= start 的项跳过；只剩 1 个有效 → 仍能跑。"""
    src = tmp_path / "main.mp4"
    src.write_bytes(b"\x00" * 128)
    dst = tmp_path / "overlaid.mp4"

    items = [
        {"item_id": "bad-1", "kind": "subtitle", "start": 5.0, "end": 3.0, "text": "x"},
        {"item_id": "bad-2", "kind": "subtitle", "start": 1.0, "end": 1.0, "text": "y"},
        {"item_id": "ok", "kind": "subtitle", "start": 0.0, "end": 2.0, "text": "保留我"},
    ]
    ffmpeg_svc.burn_packaging_track(src, items, dst)
    vf = " ".join(fake_ffmpeg["cmd"])
    assert "保留我" in vf
    # 两个 bad item 没进 filter
    assert vf.count("drawtext") == 1


def test_escape_drawtext_handles_specials():
    """drawtext 的 : ' \\ % 都要转义，避免滤镜串被吃掉。"""
    escaped = ffmpeg_svc._escape_drawtext_text("a:b'c\\d%e")
    assert "\\:" in escaped
    assert "\\'" in escaped
    assert "\\\\" in escaped
    assert "\\%" in escaped


def test_burn_packaging_requires_ffmpeg(monkeypatch, tmp_path):
    monkeypatch.setattr(ffmpeg_svc, "ffmpeg_available", lambda: False)
    src = tmp_path / "main.mp4"
    src.write_bytes(b"\x00")
    with pytest.raises(ffmpeg_svc.FFmpegError):
        ffmpeg_svc.burn_packaging_track(src, [], tmp_path / "out.mp4")
