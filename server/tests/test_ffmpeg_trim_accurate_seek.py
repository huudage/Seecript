"""stage-79 (2026-06-12) ffmpeg.trim 必须用组合 seek 防 "单镜头内复读前 0.X 秒" bug。

用户原话：「就是单镜头内存在突然对前零点几秒的视频内容进行重复的现象」

根因：早期 `-ss <start> -i src` 是 fast input seek，ffmpeg 跳到 start 前最近的关键帧后，
decoder 暖机产生的前置帧偶发会泄漏到输出（B 帧 DTS 异常 / 源已被前置裁剪过时尤甚），
表现为单镜头前 0.X 秒复读上一片段内容。

修复：reencode=True 时改为组合 seek：
  -ss (start-1.0) -i src -ss (1.0) -t dur
fast 跳到目标前 1 秒附近，再 output -ss 做 frame-accurate 精确帧 seek。
output -ss 强制 decoder 完整解到目标帧，永远精确。

这个测试只校验 cmd 构造（不真跑 ffmpeg），保证未来 refactor 不会回退到 single-input-seek。
真实 ffmpeg 端到端由 test_e2e_pipeline 在装了 ffmpeg 的机器上覆盖。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.services.video import ffmpeg as ffmpeg_svc


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, capture_output=True, text=True, check=False):
        captured["cmd"] = list(cmd)
        dst = Path(cmd[-1])
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"\x00" * 64)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(ffmpeg_svc, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(subprocess, "run", fake_run)
    return captured


def _make_src(tmp_path: Path) -> Path:
    src = tmp_path / "src.mp4"
    src.write_bytes(b"\x00" * 1024)
    return src


def test_trim_reencode_uses_combined_seek_to_avoid_leading_replay(fake_ffmpeg, tmp_path):
    """reencode=True 必须有两个 -ss：一个在 -i 前（fast），一个在 -i 后（accurate）。"""
    src = _make_src(tmp_path)
    dst = tmp_path / "out.mp4"
    ffmpeg_svc.trim(src, dst, start=5.0, duration=3.0, reencode=True)

    cmd = fake_ffmpeg["cmd"]
    # 找出 -ss 在 cmd 中的所有位置
    ss_positions = [i for i, tok in enumerate(cmd) if tok == "-ss"]
    i_position = cmd.index("-i")

    assert len(ss_positions) == 2, f"reencode=True 必须组合 seek（两个 -ss）；实际 cmd={cmd}"
    assert ss_positions[0] < i_position, "第一个 -ss 必须在 -i 前（fast pre-seek）"
    assert ss_positions[1] > i_position, "第二个 -ss 必须在 -i 后（accurate output seek）"

    # 前置 seek = start - 1.0，output seek 把差额补上，二者相加 = 原 start
    pre_ss = float(cmd[ss_positions[0] + 1])
    fine_ss = float(cmd[ss_positions[1] + 1])
    assert pre_ss + fine_ss == pytest.approx(5.0, abs=0.001)
    assert pre_ss == pytest.approx(4.0, abs=0.001)
    assert fine_ss == pytest.approx(1.0, abs=0.001)


def test_trim_reencode_start_less_than_1s_clamps_pre_seek_to_zero(fake_ffmpeg, tmp_path):
    """start < 1.0 时 pre_ss 必须 clamp 到 0（不能给负数 -ss）；fine_ss 接管全部偏移。"""
    src = _make_src(tmp_path)
    dst = tmp_path / "out.mp4"
    ffmpeg_svc.trim(src, dst, start=0.4, duration=2.0, reencode=True)

    cmd = fake_ffmpeg["cmd"]
    ss_positions = [i for i, tok in enumerate(cmd) if tok == "-ss"]
    pre_ss = float(cmd[ss_positions[0] + 1])
    fine_ss = float(cmd[ss_positions[1] + 1])
    assert pre_ss == pytest.approx(0.0)
    assert fine_ss == pytest.approx(0.4, abs=0.001)


def test_trim_reencode_start_zero_both_zero(fake_ffmpeg, tmp_path):
    """从 0 开始切：pre=0, fine=0，仍然两个 -ss 保持 cmd 结构稳定。"""
    src = _make_src(tmp_path)
    dst = tmp_path / "out.mp4"
    ffmpeg_svc.trim(src, dst, start=0.0, duration=5.0, reencode=True)

    cmd = fake_ffmpeg["cmd"]
    ss_positions = [i for i, tok in enumerate(cmd) if tok == "-ss"]
    assert len(ss_positions) == 2
    assert float(cmd[ss_positions[0] + 1]) == pytest.approx(0.0)
    assert float(cmd[ss_positions[1] + 1]) == pytest.approx(0.0)


def test_trim_no_reencode_keeps_fast_seek_only(fake_ffmpeg, tmp_path):
    """reencode=False 走 -c copy，必须保留单 -ss fast seek（关键帧切片，accurate seek 无意义）。"""
    src = _make_src(tmp_path)
    dst = tmp_path / "out.mp4"
    ffmpeg_svc.trim(src, dst, start=5.0, duration=3.0, reencode=False)

    cmd = fake_ffmpeg["cmd"]
    ss_positions = [i for i, tok in enumerate(cmd) if tok == "-ss"]
    i_position = cmd.index("-i")
    assert len(ss_positions) == 1, "reencode=False 只能有一个 -ss"
    assert ss_positions[0] < i_position, "reencode=False 用 fast input seek（-ss 在 -i 前）"
    assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy"


def test_trim_reencode_with_canvas_keeps_combined_seek(fake_ffmpeg, tmp_path):
    """canvas 参数路径也必须保持组合 seek（不能因为加了 -vf 就忘了 output seek）。"""
    src = _make_src(tmp_path)
    dst = tmp_path / "out.mp4"
    ffmpeg_svc.trim(src, dst, start=10.0, duration=4.0, reencode=True, canvas=(1080, 1920))

    cmd = fake_ffmpeg["cmd"]
    ss_positions = [i for i, tok in enumerate(cmd) if tok == "-ss"]
    i_position = cmd.index("-i")
    assert len(ss_positions) == 2
    assert ss_positions[0] < i_position < ss_positions[1]
    # canvas filter 必须在 output 端，且组合 seek 不破坏 -vf 注入
    assert "-vf" in cmd
    vf_idx = cmd.index("-vf")
    assert vf_idx > i_position, "-vf 必须在 -i 之后"
