"""FastAPI application entry.

What this file is responsible for (Single Responsibility):
- Construct the FastAPI app instance
- Register middleware (CORS, request-id, exception handler)
- Mount routers (one per business module)

What this file is NOT responsible for:
- Business logic (lives in services/)
- I/O contracts (lives in schemas.py)
- LLM/ASR client implementations (lives in services/llm_client.py / services/asr_client.py)
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import get_settings
from .routers import asr, decompose, edit, gap, library, material, packaging, plan, render
from .schemas import ErrorResponse, HealthResponse


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
def _setup_logging() -> None:
    settings = get_settings()
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s [%(threadName)s] - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# --------------------------------------------------------------------------
# Lifespan: replaces deprecated @app.on_event("startup"/"shutdown")
# --------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_logging()
    log = logging.getLogger("seecript.boot")
    settings = get_settings()
    log.info(
        "Seecript v%s booting on %s:%s | LLM=%s | ASR=%s",
        __version__,
        settings.host,
        settings.port,
        settings.llm_provider,
        settings.asr_provider,
    )
    yield
    log.info("Seecript shutting down.")


# --------------------------------------------------------------------------
# App factory (testable)
# --------------------------------------------------------------------------
def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Seecript API",
        description="爆款结构迁移引擎后端：样例拆解、结构迁移、素材缺口补全、视频重组。",
        version=__version__,
        lifespan=lifespan,
        # Disable docs in production for a tiny security gain; enable locally.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
    )

    # ---- CORS ----
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- Request-ID + access logging middleware ----
    log_access = logging.getLogger("seecript.access")

    @app.middleware("http")
    async def add_trace_id(request: Request, call_next):
        trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:12]
        request.state.trace_id = trace_id
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:  # pragma: no cover - tested via 500 handler
            log_access.exception("[%s] %s %s -> 500 %s", trace_id, request.method, request.url.path, exc)
            return JSONResponse(
                status_code=500,
                content=ErrorResponse(detail="internal server error", code="UNCAUGHT", trace_id=trace_id).model_dump(),
            )
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        response.headers["X-Trace-Id"] = trace_id
        log_access.info(
            "[%s] %s %s -> %s (%dms)", trace_id, request.method, request.url.path, response.status_code, elapsed_ms
        )
        return response

    # ---- Cross-Origin Isolation (predict 后续需要 ffmpeg.wasm/Remotion 浏览器加载 wasm) ----
    # 保留 COOP/COEP；旧版 vendor/ffmpeg 已删，新前端如要在浏览器跑 wasm 仍需 crossOriginIsolated。
    @app.middleware("http")
    async def add_cross_origin_isolation(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/api/"):
            return response
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Embedder-Policy", "credentialless")
        return response

    # ---- Routes ----
    @app.get("/api/health", response_model=HealthResponse, tags=["meta"])
    async def health() -> HealthResponse:
        return HealthResponse(
            status="healthy",
            version=__version__,
            llm_provider=settings.llm_provider,
            t2v_provider=settings.t2v_provider,
            asr_provider=settings.asr_provider,
        )

    # 阶段 1：7 个业务路由 + asr。所有路由的 prefix 都是 /api/*。
    app.include_router(asr.router, prefix="/api/asr", tags=["asr"])
    app.include_router(library.router, prefix="/api", tags=["library"])
    app.include_router(decompose.router, prefix="/api", tags=["decompose"])
    app.include_router(material.router, prefix="/api", tags=["material"])
    app.include_router(gap.router, prefix="/api", tags=["gap"])
    app.include_router(plan.router, prefix="/api", tags=["plan"])
    app.include_router(packaging.router, prefix="/api", tags=["packaging"])
    app.include_router(render.router, prefix="/api", tags=["render"])
    app.include_router(edit.router, prefix="/api", tags=["edit"])

    # ---- Static: 样例素材 ----
    # 把 server/samples/ 暴露成 /samples/...；前端 cover_url / shot 缩略图 / video.mp4
    # 都通过这个前缀加载。samples 目录不存在时跳过挂载（避免本地未拷贝样例就启不来）。
    samples_dir = settings.log_dir.parent / "samples"
    if samples_dir.exists():
        app.mount("/samples", StaticFiles(directory=str(samples_dir)), name="samples")
    else:
        logging.getLogger("seecript.boot").warning(
            "[boot] %s 不存在，/samples 静态路由未挂载（cover/缩略图将 404）",
            samples_dir,
        )

    # ---- Static: 用户上传 + 渲染产物 ----
    # /uploads/{session_id}/...  → server/var/uploads/...
    # /outputs/{job_id}/...      → server/var/outputs/...
    # 先确保目录存在，再挂载，避免 StaticFiles 启动检查失败。
    uploads_dir = settings.log_dir.parent / "var" / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")

    outputs_dir = settings.log_dir.parent / "var" / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/outputs", StaticFiles(directory=str(outputs_dir)), name="outputs")

    return app


# uvicorn entry point: `uvicorn app.main:app`
app = create_app()
