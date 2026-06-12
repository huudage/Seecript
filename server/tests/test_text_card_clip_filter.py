"""stage-80 (2026-06-12) text_card_clip 滤镜串引号回归。

bug：bounce_word 动画的 y 表达式包含逗号
    `((h-text_h)/2-text_h*0.6)+if(lt(t,0.7),sin(2*PI*t*3)*15*(1-t/0.7),0)`
原 y= 字段未加引号，ffmpeg 滤镜串解析时把逗号当 filter 分隔符切成 4 段，
最终报 `No such filter: '0.7)'`。生产 step2 预览半数 text_card scene 因此失败
回退到 color 占位 → 用户看到黑屏 0:02 mp4。

修复：text_card_clip 的 main y / sub y 字段都用 `'...'` 包起来（alpha 早就这么做了）。

本测试不实际跑 ffmpeg（需要二进制），而是 mock subprocess.run 拦截命令行，
断言 `-vf` 参数里的 y 值用单引号包了——结构层面修复就够了，跑不跑 ffmpeg 是另一层。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _spec(animation: str = "fade_in", layout: str = "center") -> dict:
    return {
        "main_text": "测试主标",
        "sub_text": "副标说明",
        "duration_seconds": 4.0,
        "bg_mode": "solid",
        "bg_color": "#0F172A",
        "text_color": "#FFFFFF",
        "accent_color": "#22D3EE",
        "font_family": "bold_sans",
        "layout": layout,
        "animation": animation,
        "emoji_decor": [],
    }


@pytest.mark.parametrize("animation", ["fade_in", "typewriter", "bounce_word", "zoom_pop"])
def test_text_card_clip_y_field_is_quoted(animation, tmp_path, monkeypatch):
    """所有动画路径下 main/sub y 字段都必须用 `'...'` 包起来——
    bounce_word 的 y 含逗号，不引就被切成 filter 分隔符。
    """
    from app.services.video import ffmpeg as ffmpeg_svc

    captured_cmd: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        captured_cmd.append(list(cmd))
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        return result

    monkeypatch.setattr(ffmpeg_svc, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(ffmpeg_svc, "find_cjk_font", lambda: None)
    monkeypatch.setattr("subprocess.run", fake_run)

    out = tmp_path / "card.mp4"
    ffmpeg_svc.text_card_clip(_spec(animation=animation), out, width=480, height=854)

    assert captured_cmd, "subprocess.run 没被调用"
    cmd = captured_cmd[0]
    vf_idx = cmd.index("-vf")
    vf_value = cmd[vf_idx + 1]

    # 找到 main drawtext（第一段）的 y 字段
    # 形如：drawtext=text='...':...:y='<expr>':alpha='...'
    # 简单断言 y= 后面紧跟单引号
    assert ":y='" in vf_value, (
        f"animation={animation} 的 main y 字段没用单引号包！vf=\n{vf_value}"
    )
    # 两个 drawtext（main + sub）都该被 quote（spec 提供了 sub_text）
    y_quoted_count = vf_value.count(":y='")
    assert y_quoted_count >= 2, (
        f"animation={animation} 期望 main+sub 两个 y 都被 quote，实际 {y_quoted_count} 处。vf=\n{vf_value}"
    )


def test_text_card_clip_bounce_word_no_unquoted_comma_in_y(tmp_path, monkeypatch):
    """bounce_word 动画 y 含 `,`：必须落在引号内，不能裸露在 filter 串里。

    具体校验：把 vf 里所有 `'...'` 引号区段抠掉后，剩余文本不应再含 `0.7)` 这种
    bounce_word y 表达式片段——确保它确实被引号保护。
    """
    from app.services.video import ffmpeg as ffmpeg_svc

    captured_cmd: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        captured_cmd.append(list(cmd))
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        return result

    monkeypatch.setattr(ffmpeg_svc, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(ffmpeg_svc, "find_cjk_font", lambda: None)
    monkeypatch.setattr("subprocess.run", fake_run)

    out = tmp_path / "card.mp4"
    ffmpeg_svc.text_card_clip(_spec(animation="bounce_word"), out)

    cmd = captured_cmd[0]
    vf_value = cmd[cmd.index("-vf") + 1]

    # 把单引号区段全部替换成 ___QUOTED___，剩余文本不能含 `0.7)` / `lt(t,` 等
    # bounce_word y 子表达式（这些字符在引号外出现就是 bug）
    import re
    stripped = re.sub(r"'[^']*'", "___QUOTED___", vf_value)
    assert "0.7)" not in stripped, (
        f"bounce_word y 表达式片段 `0.7)` 未被引号保护！未保护片段:\n{stripped}"
    )
    assert "sin(" not in stripped, (
        f"bounce_word y 表达式 sin(...) 未被引号保护！未保护片段:\n{stripped}"
    )
