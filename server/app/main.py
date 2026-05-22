"""FastAPI application entry.

What this file is responsible for (Single Responsibility):
- Construct the FastAPI app instance
- Register middleware (CORS, request-id, exception handler)
- Mount routers (one per business module)
- Serve the static frontend so a single uvicorn process powers the whole demo

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
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import get_settings
from .routers import asr, comments, persona, qa, script, seo, skeleton, t2v
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
        "Seecript v%s booting on %s:%s | LLM=%s | ASR=%s | T2V=%s | static=%s",
        __version__,
        settings.host,
        settings.port,
        settings.llm_provider,
        settings.asr_provider,
        settings.t2v_provider,
        settings.static_root.resolve(),
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
        description="AI 副驾后端：人设生成、爆款拆解、引导式问答、原创分镜脚本、SEO 元数据、评论分拣。",
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

    # ---- Cross-Origin Isolation for ffmpeg.wasm ----
    # ffmpeg.wasm 0.12 needs SharedArrayBuffer, which requires `crossOriginIsolated`.
    # We use COEP=credentialless so remote CDN assets (jsdelivr, Google Fonts) can load
    # without each having to set Cross-Origin-Resource-Policy. Tradeoff: those requests
    # are sent without cookies, which is fine for static CDNs.
    #
    # This is a *blanket* policy; if you ever embed third-party iframes that need cookies
    # (e.g. payment SDK), you'll need a route-specific exception.
    @app.middleware("http")
    async def add_cross_origin_isolation(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        # Only HTML/JS documents need the isolation; static audio/api can skip.
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
            asr_provider=settings.asr_provider,
            t2v_provider=settings.t2v_provider,
        )

    app.include_router(persona.router, prefix="/api/persona", tags=["persona"])
    app.include_router(skeleton.router, prefix="/api/skeleton", tags=["skeleton"])
    app.include_router(qa.router, prefix="/api/qa", tags=["qa"])
    app.include_router(script.router, prefix="/api/script", tags=["script"])
    app.include_router(seo.router, prefix="/api/seo", tags=["seo"])
    app.include_router(comments.router, prefix="/api/comments", tags=["comments"])
    app.include_router(asr.router, prefix="/api/asr", tags=["asr"])
    app.include_router(t2v.router, prefix="/api/t2v", tags=["t2v"])

    # ---- Static frontend (mounted last so /api/* takes precedence) ----
    # 极速版 ASR 直传 base64，已经不再需要 /asr-tmp/ 公网回源路径，省一处配置。
    static_root: Path = settings.static_root.resolve()
    if static_root.exists():
        # html=True so / falls back to index.html
        app.mount("/", StaticFiles(directory=str(static_root), html=True), name="static")
    else:
        logging.getLogger("seecript.boot").warning(
            "static_root %s does not exist; frontend will not be served", static_root
        )

    return app


# uvicorn entry point: `uvicorn app.main:app`
app = create_app()
