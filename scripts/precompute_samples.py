"""Pre-compute SampleManifest for system samples — one-shot offline.

跑 decompose_agent.decompose() 对 server/samples/<id>/video.mp4 三个内置样例：
- PySceneDetect 切真实镜头
- ffmpeg 抽每个镜头的中点帧重新写 shot-NN.jpg（旧的 8 张是手工切的，需要对齐真切片）
- ASR 真模型转写口播（除 motion_graph 自动跳过）
- 多模态 LLM 帧打标 + 段落分析（按 video_type 三选一）
- 结果写入 server/samples/<id>/manifest.json 供 library 路由直接加载

用法（已激活 venv）：
    cd D:/Seecript
    python scripts/precompute_samples.py            # 跑全部 3 个
    python scripts/precompute_samples.py sample-marketing-01   # 只跑指定 id

如果 .env 里 ASR_PROVIDER=mock 或者某个模型 key 缺失，相应步骤自动回落 mock 数据。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = REPO_ROOT / "server"
sys.path.insert(0, str(SERVER_DIR))

from app.routers.library import _SYSTEM_LIBRARY  # noqa: E402
from app.schemas import SampleManifest  # noqa: E402
from app.services.agent.decompose_agent import decompose  # noqa: E402
from app.services.video import ffmpeg  # noqa: E402
from app.services.video.scene_detect import detect_shots  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("precompute")


SAMPLES_ROOT = SERVER_DIR / "samples"


def _regen_shot_thumbnails(sample_id: str, video_path: Path) -> int:
    """先按 PySceneDetect 真实切片重新抽取 shot-NN.jpg，让缩略图跟 manifest 一一对应。

    旧的手工 shot-00..07.jpg 数量可能跟真切片对不上，多模态 LLM 拿到的图就乱了。
    """
    sample_dir = SAMPLES_ROOT / sample_id
    sample_dir.mkdir(parents=True, exist_ok=True)

    shots = detect_shots(video_path)
    if not shots:
        log.warning("[%s] PySceneDetect 0 shots, 跳过缩略图重抽", sample_id)
        return 0

    # 清掉旧 shot-NN.jpg
    for old in sample_dir.glob("shot-*.jpg"):
        old.unlink()

    for sh in shots:
        midpoint = sh.start + sh.duration / 2
        dst = sample_dir / f"shot-{sh.index:02d}.jpg"
        try:
            ffmpeg.extract_frame(video_path, midpoint, dst)
        except Exception as exc:
            log.warning("[%s] extract_frame shot-%02d 失败：%s", sample_id, sh.index, exc)
    log.info("[%s] 重抽 %d 张缩略图", sample_id, len(shots))
    return len(shots)


async def _precompute_one(sample_id: str) -> bool:
    item = next((it for it in _SYSTEM_LIBRARY if it.id == sample_id), None)
    if item is None:
        log.error("sample %s 不在 _SYSTEM_LIBRARY", sample_id)
        return False

    video_path = SAMPLES_ROOT / sample_id / "video.mp4"
    if not video_path.is_file():
        log.error("[%s] 视频不存在：%s", sample_id, video_path)
        return False

    log.info("=" * 60)
    log.info("[%s] 开始预拆解：%s (%s)", sample_id, item.title, item.video_type)

    _regen_shot_thumbnails(sample_id, video_path)

    manifest: SampleManifest = await decompose(
        sample_id,
        video_path=video_path,
        title=item.title,
        video_type=item.video_type,
    )

    out = SAMPLES_ROOT / sample_id / "manifest.json"
    out.write_text(
        json.dumps(manifest.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info(
        "[%s] ✅ 写入 manifest.json  shots=%d  sections=%d  has_voice=%s  duration=%.1fs",
        sample_id,
        len(manifest.shots),
        len(manifest.sections),
        manifest.has_voice,
        manifest.duration_seconds,
    )
    return True


async def _main(ids: Iterable[str]) -> int:
    failed: list[str] = []
    for sid in ids:
        try:
            ok = await _precompute_one(sid)
            if not ok:
                failed.append(sid)
        except Exception as exc:
            log.exception("[%s] 预拆解异常：%s", sid, exc)
            failed.append(sid)

    if failed:
        log.error("失败：%s", failed)
        return 1
    log.info("✅ 全部预拆解完成")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    target_ids = args if args else [it.id for it in _SYSTEM_LIBRARY]
    raise SystemExit(asyncio.run(_main(target_ids)))
