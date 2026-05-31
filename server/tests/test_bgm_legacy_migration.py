"""BGMConfig 字段迁移：旧 start_offset → video_anchor_seconds 翻号。

老 plan.bgm 用 `start_offset>=0` 表示「跳过 BGM 开头 N 秒」。
新 schema 改为 `video_anchor_seconds`：
- 正值 = 视频先静音 N 秒再起 BGM
- 负值 = 跳过 BGM 开头 N 秒

迁移规则：start_offset → -video_anchor_seconds（旧→新方向相反）。
"""
from __future__ import annotations

from app.schemas import BGMConfig


def test_legacy_start_offset_migrates_to_negative_anchor():
    cfg = BGMConfig.model_validate({
        "asset_id": "bgm-x",
        "track_url": "/assets/u/bgm/x.mp3",
        "start_offset": 2.5,
        "volume": 0.4,
    })
    assert cfg.video_anchor_seconds == -2.5
    # 旧字段不应保留
    assert not hasattr(cfg, "start_offset")


def test_zero_legacy_start_offset_yields_zero_anchor():
    cfg = BGMConfig.model_validate({
        "asset_id": "bgm-x",
        "track_url": "/assets/u/bgm/x.mp3",
        "start_offset": 0,
        "volume": 0.4,
    })
    assert cfg.video_anchor_seconds == 0.0


def test_new_anchor_takes_precedence_over_legacy_field():
    cfg = BGMConfig.model_validate({
        "asset_id": "bgm-x",
        "track_url": "/assets/u/bgm/x.mp3",
        "start_offset": 1.0,
        "video_anchor_seconds": 4.0,
        "volume": 0.4,
    })
    assert cfg.video_anchor_seconds == 4.0


def test_default_anchor_is_zero_when_no_legacy_field():
    cfg = BGMConfig.model_validate({
        "asset_id": "bgm-x",
        "track_url": "/assets/u/bgm/x.mp3",
        "volume": 0.4,
    })
    assert cfg.video_anchor_seconds == 0.0
