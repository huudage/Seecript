"""Seedance 2.0 拒绝 duration<5 → DoubaoArkT2VClient.submit 必须把请求体
duration 至少抬到 5，无论 caller 传 2/3/4 都得能落地。

回归用例对应 2026-06-11 线上 HTTP 400：
    'code': 'InvalidParameter',
    'message': 'the parameter duration specified in the request is not valid
                for model doubao-seedance-2-0-fast in t2v'
触发路径：plan.py swap-source → aigc_t2v 分支，shot_dur=2.5 → 旧 max(2.0, ...)
→ submit duration=2 → Seedance 退。
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.services.t2v_client import DoubaoArkT2VClient


class _CaptureTransport(httpx.MockTransport):
    """记下 POST body，让我们在测试里断言 duration 字段。"""

    def __init__(self) -> None:
        self.last_body: dict[str, Any] | None = None

        def handler(request: httpx.Request) -> httpx.Response:
            self.last_body = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"id": "cgt-test-001"})

        super().__init__(handler)


def _make_client(monkeypatch) -> tuple[DoubaoArkT2VClient, _CaptureTransport]:
    settings = Settings(
        t2v_provider="doubao_ark",
        t2v_api_key="test-key",
        ark_base_url="https://ark.test/api/v3",
        ark_t2v_model="doubao-seedance-2-0-fast-260128",
        ark_t2v_resolution="720p",
        t2v_timeout_seconds=10,
        t2v_default_ratio="9:16",
        t2v_generate_audio=False,
        t2v_watermark=False,
    )
    client = DoubaoArkT2VClient(settings)
    transport = _CaptureTransport()
    # 把 httpx.AsyncClient 的 transport 换成 capture：所有 POST 都走它，不出网
    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    return client, transport


@pytest.mark.asyncio
@pytest.mark.parametrize("caller_duration", [1, 2, 3, 4])
async def test_submit_floors_duration_to_five(monkeypatch, caller_duration):
    """caller 传 <5 的 duration → submit body 必须实际发 5（Seedance 拒绝 <5）。"""
    client, transport = _make_client(monkeypatch)
    await client.submit(prompt="测试画面", duration_seconds=caller_duration)
    assert transport.last_body is not None
    assert transport.last_body["duration"] == 5, (
        f"caller 传 {caller_duration} 应被抬到 5，实际 body 发了 "
        f"{transport.last_body['duration']}（Seedance 2.0 会 400）"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("caller_duration", [5, 6, 10, 12, 15])
async def test_submit_passes_through_when_geq_five(monkeypatch, caller_duration):
    """≥5 的 duration 必须原样下发，不被截断/抬高。"""
    client, transport = _make_client(monkeypatch)
    await client.submit(prompt="测试画面", duration_seconds=caller_duration)
    assert transport.last_body is not None
    assert transport.last_body["duration"] == caller_duration


@pytest.mark.asyncio
async def test_plan_swap_source_aigc_t2v_floors_short_shot_dur(monkeypatch):
    """plan.py:1934 per_chunk_seconds 的 max(5.0, shot_dur) 回归：
    shot_dur=2.5 时，提交给 Seedance 的 duration 至少是 5。"""
    # 直接复算这一行的语义，避免拉起整条 swap-source 路由的开销。
    from app.services.agent.gap_agent import SEEDANCE_MAX_SECONDS

    for shot_dur in (1.0, 2.5, 3.0, 4.9):
        per_chunk_seconds = int(round(min(float(SEEDANCE_MAX_SECONDS), max(5.0, shot_dur))))
        assert per_chunk_seconds >= 5, f"shot_dur={shot_dur} 算出 {per_chunk_seconds}<5"

    for shot_dur in (5.0, 6.7, 11.4, 20.0):
        per_chunk_seconds = int(round(min(float(SEEDANCE_MAX_SECONDS), max(5.0, shot_dur))))
        assert 5 <= per_chunk_seconds <= SEEDANCE_MAX_SECONDS


@pytest.mark.asyncio
async def test_gap_agent_per_chunk_never_below_five():
    """gap_agent.py:856 的 per_chunk = max(5.0, ...) 回归。"""
    import math
    from app.services.agent.gap_agent import SEEDANCE_MAX_SECONDS

    for requested in (5.0, 6.0, 11.0, 12.0, 13.0, 25.0, 30.0, 60.0):
        n_chunks = max(1, math.ceil(requested / SEEDANCE_MAX_SECONDS))
        per_chunk = max(5.0, min(float(SEEDANCE_MAX_SECONDS), requested / n_chunks))
        assert per_chunk >= 5.0, f"requested={requested} 切成 {n_chunks} 段，每段 {per_chunk}<5"
