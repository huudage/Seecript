"""Stage 3 · 视频渲染流水线。

`pipeline.run_pipeline(job_id, plan)` 负责把一个 Plan 走完整链路：
  prepare → ffmpeg_concat → seedance_extend → remotion_render → ffmpeg_overlay → finalize

每一步都做 graceful degradation：FFmpeg/Remotion/Seedance 不可用时退到 mock 占位文件，
保证比赛 demo 不会因为环境缺依赖就崩。
"""
from .pipeline import RenderResult, run_pipeline
from .seedance_chain import SeedanceChainError, extend_with_seedance

__all__ = ["RenderResult", "run_pipeline", "SeedanceChainError", "extend_with_seedance"]
