"""Seedance 链式生成（>12s 自动分段）直测。

覆盖 gap_agent._fill_with_seedance 的分段策略与链式衔接：
- ≤12s 单段，不触发链式
- >12s 按 ceil(requested/12) 切 N 段，顺序生成，每段 ≤12s
- 链式时前一段尾帧抽取失败 → 立即中断（不浪费配额），返回 warn + 已成功段

mock 说明：
- T2V_MOCK_DURATION_SECONDS=0 让 MockT2VClient.query 立即 succeeded
- 尾帧抽取走真 ffmpeg + httpx 下载，对 mock 的 /aigc/*.mp4 URL 必然失败，
  所以链式测试里 monkeypatch `_extract_tail_frame_data_url` 返回假 data URL，
  才能让 N 段全部跑完。
"""
from __future__ import annotations

import pytest

from app.schemas import Gap
from app.services.agent import gap_agent
from app.services.agent.gap_agent import SEEDANCE_MAX_SECONDS, fill_gap


@pytest.fixture(autouse=True)
def _instant_mock_t2v(monkeypatch):
    """让 mock T2V query 立即成功，避免每段等 8s。"""
    monkeypatch.setenv("T2V_MOCK_DURATION_SECONDS", "0")
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _gap(section="development", section_id="sec-1") -> Gap:
    return Gap(
        gap_id=f"gap-{section}-0",
        section=section,  # type: ignore[arg-type]
        section_id=section_id,
        slot_index=0,
        requirement="测试缺口需求",
        status="miss",
        impact="medium",
    )


@pytest.mark.asyncio
async def test_single_chunk_when_under_limit():
    """≤12s 不分段：chunks_count=1，video_urls 单元素。"""
    result = await fill_gap(
        _gap(),
        action="aigc",
        params={
            "prompt": "单段画面",
            "duration_seconds": SEEDANCE_MAX_SECONDS,  # 恰好 12s → 1 段
            "poll_interval_seconds": 0.01,
            "max_wait_seconds": 5.0,
        },
    )
    assert result.status == "ok"
    assert result.chunks_count == 1
    assert len(result.video_urls) == 1
    assert len(result.chunk_task_ids) == 1


@pytest.mark.asyncio
async def test_chained_chunks_when_over_limit(monkeypatch):
    """25s → ceil(25/12)=3 段；链式衔接成功 → 3 段全 ok。"""
    calls: list[str] = []

    async def _fake_tail(video_url: str, *, timeout: float = 60.0) -> str:
        calls.append(video_url)
        return "data:image/jpeg;base64,ZmFrZQ=="

    monkeypatch.setattr(gap_agent, "_extract_tail_frame_data_url", _fake_tail)

    result = await fill_gap(
        _gap(),
        action="aigc",
        params={
            "prompt": "多段链式画面",
            "duration_seconds": 25,  # → 3 段，每段 ≈8.3s ≤12s
            "poll_interval_seconds": 0.01,
            "max_wait_seconds": 5.0,
        },
    )
    assert result.status == "ok", result.note
    assert result.chunks_count == 3, f"应切 3 段，实际 {result.chunks_count}"
    assert len(result.video_urls) == 3
    assert len(result.chunk_task_ids) == 3
    # 链式需要在前 N-1 段后各抽一次尾帧 → 2 次
    assert len(calls) == 2, f"尾帧抽取次数应为 2，实际 {len(calls)}"
    # new_material_id 兼容旧前端：取首段 task_id
    assert result.new_material_id == result.chunk_task_ids[0]


@pytest.mark.asyncio
async def test_chain_aborts_when_tail_frame_fails(monkeypatch):
    """链式中尾帧抽取失败 → 第 1 段后立即中断，返回 warn + 仅 1 段成功。"""
    async def _boom(video_url: str, *, timeout: float = 60.0) -> str:
        raise RuntimeError("ffmpeg unavailable")

    monkeypatch.setattr(gap_agent, "_extract_tail_frame_data_url", _boom)

    result = await fill_gap(
        _gap(),
        action="aigc",
        params={
            "prompt": "尾帧会失败",
            "duration_seconds": 25,  # 期望 3 段
            "poll_interval_seconds": 0.01,
            "max_wait_seconds": 5.0,
        },
    )
    # 第 1 段成功但拿不到尾帧 → 无法继续 → warn（部分完成）
    assert result.status == "warn", result.note
    assert result.chunks_count == 1, f"中断后只应有 1 段，实际 {result.chunks_count}"
    assert len(result.video_urls) == 1
    assert "1/3" in (result.note or ""), f"note 应提示 1/3：{result.note!r}"


@pytest.mark.asyncio
async def test_wall_clock_cap_returns_pending(monkeypatch):
    """wall-clock 总耗时超过 total_max_wait_seconds → 早返回 warn，
    chunk_task_ids[0] 必须是 pending 段的 task_id（前端 auto-poll 用 [0]）。

    这条覆盖『nginx 180s 截断 → failed to fetch』的早返回路径。
    """
    # mock T2V 永不 succeed：始终返回 pending（让 wall-clock 触发）
    from app.services.t2v_client import MockT2VClient, QueryResult, T2VClient

    class _AlwaysPendingT2V(MockT2VClient):
        async def query(self, task_id: str) -> QueryResult:  # type: ignore[override]
            return QueryResult(task_id=task_id, status="pending", provider=self.name)

    monkeypatch.setattr(gap_agent, "get_t2v_client", lambda: _AlwaysPendingT2V())

    result = await fill_gap(
        _gap(),
        action="aigc",
        params={
            "prompt": "永远 pending",
            "duration_seconds": SEEDANCE_MAX_SECONDS,  # 单段
            "poll_interval_seconds": 0.05,
            "max_wait_seconds": 60.0,                  # 单段 max_wait 远大于 wall-clock
            "total_max_wait_seconds": 0.3,             # 极小 wall-clock，必触发早返回
        },
    )
    assert result.status == "warn", f"应早返回 warn，实际 {result.status}: {result.note}"
    assert result.video_urls == [], "未完成的段不应有 video_url"
    assert len(result.chunk_task_ids) == 1
    # 关键：chunk_task_ids[0] 必须是 pending 段的 task_id，前端才能正确 auto-poll
    assert result.chunk_task_ids[0] == result.new_material_id
    assert "自动刷新" in (result.note or ""), f"note 应提示前端继续刷：{result.note!r}"
