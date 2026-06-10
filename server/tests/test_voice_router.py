"""Voice router HTTP 烟测：单段 + 批量合成 + 删除。

mock TTS 模式下产物是合法 WAV，路由要：
1. 写入 plan.main_track[i].voiceover_url
2. 落盘到 server/var/voiceovers/<plan>/<scene>.wav
3. plan_store 持久化
4. synthesize-all 跳过空 narration、统计 ok/skipped/failures
5. voiceover_enabled=False 时 synthesize-all 返回 400
"""
from __future__ import annotations

import shutil
import time

import pytest

from app.config import get_settings
from app.schemas import AdaptedSection, ComposeSettings, Plan, Scene
from app.services.plans.store import plan_store


_TEST_PLAN_IDS: list[str] = []


def _make_plan(plan_id: str, voiceover_enabled: bool = True) -> Plan:
    return Plan(
        plan_id=plan_id,
        sample_ids=["sample-marketing-01"],
        project_id=None,
        session_id=None,
        settings=ComposeSettings(voiceover_enabled=voiceover_enabled),
        adapted_sections=[
            AdaptedSection(
                section_id="adp-opening", role="opening", theme="开场",
                content_description="hook", source_shot_indices=[0],
                order=0, duration_seconds=3.0,
            ),
            AdaptedSection(
                section_id="adp-closing", role="closing", theme="收尾",
                content_description="cta", source_shot_indices=[1],
                order=1, duration_seconds=3.0,
            ),
        ],
        main_track=[
            Scene(
                scene_id="sc-0", section="opening", source="user_material",
                source_ref="m-1", start=0.0, duration=3.0,
                narration="第一段口播文案",
            ),
            Scene(
                scene_id="sc-1", section="closing", source="user_material",
                source_ref="m-2", start=3.0, duration=3.0,
                narration="",  # 空 narration → synthesize-all 应 skip
            ),
        ],
        packaging_track=[],
        duration_seconds=6.0,
        variant="A",
    )


@pytest.fixture(autouse=True)
def cleanup_voice():
    yield
    voice_root = get_settings().log_dir.parent / "var" / "voiceovers"
    for plan_id in _TEST_PLAN_IDS:
        plan_store._plans.pop(plan_id, None)
        target = voice_root / plan_id
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
    _TEST_PLAN_IDS.clear()


def test_synthesize_one_writes_voiceover_url(client):
    plan = _make_plan(f"plan-voice-one-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    resp = client.post("/api/voice/synthesize", json={
        "plan_id": plan.plan_id,
        "scene_id": "sc-0",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # stage-49 起 voice_store 给 voiceover_url 加了 ?v=ts 缓存破坏 query；测试只匹配前缀
    assert body["voiceover_url"].split("?")[0] == f"/voiceovers/{plan.plan_id}/sc-0.wav"
    assert body["backend"] in ("mock", "volc")
    assert body["chars"] > 0

    # plan 的 scene.voiceover_url 应已回写并落盘
    refreshed = plan_store.get(plan.plan_id)
    assert refreshed is not None
    assert refreshed.main_track[0].voiceover_url == body["voiceover_url"]


def test_synthesize_one_text_override_updates_narration(client):
    plan = _make_plan(f"plan-voice-txt-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    resp = client.post("/api/voice/synthesize", json={
        "plan_id": plan.plan_id,
        "scene_id": "sc-0",
        "text": "覆盖文案：新的口播内容",
    })
    assert resp.status_code == 200, resp.text

    refreshed = plan_store.get(plan.plan_id)
    assert refreshed.main_track[0].narration == "覆盖文案：新的口播内容"


def test_synthesize_all_backfills_empty_narration(client):
    """Stage-26 起 synthesize-all 不再 skip 空 narration——为避免音轨空缺，
    自动兜底填一句话（subj/theme/content 取截 24 字，最低 '画面定格'）后照常合成。"""
    plan = _make_plan(f"plan-voice-all-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    resp = client.post("/api/voice/synthesize-all", json={"plan_id": plan.plan_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 两段都应被合成，sc-1 narration 已被路由兜底填充
    assert len(body["synthesized"]) == 2
    assert body["skipped_scene_ids"] == []
    assert body["failures"] == []
    # 验证 sc-1 的 narration 在 plan 上被兜底填了非空内容
    refreshed = plan_store.get(plan.plan_id)
    assert refreshed is not None
    sc1 = next(s for s in refreshed.main_track if s.scene_id == "sc-1")
    assert sc1.narration and sc1.narration.strip() != ""


def test_synthesize_all_rejected_when_voiceover_disabled(client):
    plan = _make_plan(f"plan-voice-off-{int(time.time() * 1000)}", voiceover_enabled=False)
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    resp = client.post("/api/voice/synthesize-all", json={"plan_id": plan.plan_id})
    assert resp.status_code == 400
    assert "voiceover_enabled" in resp.json()["detail"]


def test_delete_clears_scene_voiceover(client):
    plan = _make_plan(f"plan-voice-del-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    # 先合成一段
    client.post("/api/voice/synthesize", json={
        "plan_id": plan.plan_id, "scene_id": "sc-0",
    })
    refreshed = plan_store.get(plan.plan_id)
    assert refreshed.main_track[0].voiceover_url is not None

    resp = client.delete(f"/api/voice/{plan.plan_id}/sc-0")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["main_track"][0]["voiceover_url"] is None


def test_synthesize_truncated_when_text_exceeds_duration(client):
    """文案明显超过 scene.duration 时：mock 合成 12s wav，scene 仅 3s
    → 加速到 1.15× 仍超 → truncated 标记应为 True。"""
    plan = _make_plan(f"plan-voice-trunc-{int(time.time() * 1000)}")
    # 用一段长 narration（≈40 字）触发 mock 12s 上限
    plan.main_track[0].narration = "这是一段被故意拉长到接近极限的口播文案用来触发对齐截尾逻辑测试"
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    resp = client.post("/api/voice/synthesize", json={
        "plan_id": plan.plan_id, "scene_id": "sc-0",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["truncated"] is True, f"长文案应触发截尾标记，实际 body={body}"


def test_synthesize_all_returns_truncated_scene_ids(client):
    plan = _make_plan(f"plan-voice-trunc-all-{int(time.time() * 1000)}")
    plan.main_track[0].narration = "这是一段被故意拉长到接近极限的口播文案用来触发对齐截尾逻辑测试"
    plan.main_track[1].narration = "短文案"  # 不会触发截尾
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    resp = client.post("/api/voice/synthesize-all", json={"plan_id": plan.plan_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "sc-0" in body["truncated_scene_ids"]
    assert "sc-1" not in body["truncated_scene_ids"]


def test_synthesize_dedupes_repetition_padding_before_tts(client):
    """用户报障：TTS 会重复某几个字硬凑时长。
    防御性 dedupe 在 voice 路由入口压掉这类输入——scene.narration 应回写为干净文案。"""
    plan = _make_plan(f"plan-voice-dedupe-{int(time.time() * 1000)}")
    # 三种典型『凑时长』产物
    plan.main_track[0].narration = "来来来来来看一下这款咖啡"  # 单字 5 连
    plan.main_track[1].narration = "看看看看就知道了。看看看看就知道了。"  # 短语+整句双重重复
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    resp = client.post("/api/voice/synthesize-all", json={"plan_id": plan.plan_id})
    assert resp.status_code == 200, resp.text

    refreshed = plan_store.get(plan.plan_id)
    sc0 = refreshed.main_track[0].narration
    sc1 = refreshed.main_track[1].narration
    # 5 连应被压成 2 个
    assert "来来来来来" not in sc0, f"5 连字应被 dedupe，实际：{sc0!r}"
    assert sc0.count("来") <= 2, f"『来』应不超 2 个，实际：{sc0!r}"
    # 整句重复应只保留 1 次
    assert sc1.count("看看就知道了") == 1, f"整句应去重，实际：{sc1!r}"


def test_synthesize_one_dedupes_text_override(client):
    """单段合成时 text override 也要走 dedupe，scene.narration 显示与音频一致。"""
    plan = _make_plan(f"plan-voice-dedupe-one-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    resp = client.post("/api/voice/synthesize", json={
        "plan_id": plan.plan_id,
        "scene_id": "sc-0",
        "text": "试试试试这款奶茶",
    })
    assert resp.status_code == 200, resp.text

    refreshed = plan_store.get(plan.plan_id)
    sc0 = refreshed.main_track[0].narration
    assert "试试试试" not in sc0, f"4 连应被 dedupe，实际：{sc0!r}"
    assert sc0.count("试") <= 2
