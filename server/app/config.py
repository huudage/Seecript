"""Application configuration loader.

Why this module exists:
- Centralizes all environment-variable parsing so business code never reads `os.environ` directly.
- Uses Pydantic BaseSettings to enforce type safety and provide sensible defaults.
- Allows hot reload during local dev (uvicorn --reload picks up .env changes on restart).

Design notes:
- Following Dependency Inversion: business code depends on `Settings`, not on `os` directly.
- `lru_cache` on `get_settings()` so the same Settings object is reused across the app.
"""
from functools import lru_cache
from pathlib import Path
from typing import List, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Resolve project paths once at import time.
# Layout: <project_root>/server/app/config.py
_THIS_FILE = Path(__file__).resolve()
SERVER_DIR = _THIS_FILE.parent.parent          # .../seecript/server
PROJECT_ROOT = SERVER_DIR.parent               # .../seecript
DEFAULT_STATIC_ROOT = PROJECT_ROOT             # frontend lives at seecript/*.html
DEFAULT_ENV_FILE = SERVER_DIR / ".env"


class Settings(BaseSettings):
    """All runtime configuration. Backed by environment variables and an optional `.env` file."""

    # === Server ===
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8090)

    # === LLM ===
    llm_provider: Literal["mock", "deepseek"] = Field(default="mock")
    deepseek_api_key: str = Field(default="")
    deepseek_base_url: str = Field(default="https://api.deepseek.com")
    deepseek_model: str = Field(default="deepseek-chat")
    llm_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    llm_timeout_seconds: int = Field(default=60, ge=5, le=300)
    llm_max_tokens: int = Field(default=2048, ge=128, le=8192)
    # 「拆解骨架」JSON（hook/body/cta/template）较长；沿用全局 max_tokens 时易被截断导致非合法 JSON。
    llm_skeleton_max_tokens: int = Field(default=4096, ge=512, le=8192)
    # 人设：3 套方案 × 多字段，输出偏长。
    llm_persona_max_tokens: int = Field(default=3072, ge=512, le=8192)
    # 原创脚本：scenes + full_text 重复叙事，最易截断。
    llm_script_max_tokens: int = Field(default=6144, ge=512, le=8192)
    # 标题车间：5+ 标题 + 简介 + 标签簇。
    llm_seo_max_tokens: int = Field(default=3072, ge=512, le=8192)
    # 评论分拣：高价值多条 × 三种语气回复，JSON 体积大。
    llm_comments_max_tokens: int = Field(default=4096, ge=512, le=8192)
    # 引导问答：单轮 JSON 较小；默认沿用 llm_max_tokens，单独可调以便与长上下文路由区分。
    llm_qa_max_tokens: int = Field(default=2048, ge=512, le=8192)

    # === ASR (Doubao 极速版 / turbo / flash) ===
    # 极速版 = 一次请求拿结果 + 支持 base64 inline 上传 → 不再需要公网 URL（PUBLIC_BASE_URL 已废弃）。
    asr_provider: Literal["mock", "doubao"] = Field(default="mock")
    doubao_api_key: str = Field(default="")
    doubao_resource_id: str = Field(default="volc.bigasr.auc_turbo")
    doubao_recognize_url: str = Field(
        default="https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
    )
    # 极速版上限 100MB / 2h，但我们再按出口带宽做收敛（推荐 ≤ 20MB），timeout 60s 给足余量。
    asr_timeout_seconds: int = Field(default=60, ge=10, le=300)

    # === T2V（智谱清影，v0.9 起接入；第 7 个 AI 干预点）===
    # 默认 mock：开箱即用；切到 zhipu 需在 .env 设 ZHIPU_API_KEY。
    # 默认模型 cogvideox-3：与开放平台文档/体验中心主推一致（5s 或 10s、fps 30/60、约 1 元/次）。
    # 仍可通过 ZHIPU_VIDEO_MODEL=cogvideox-2 切回低价 6 秒方案（0.5 元/次），详见 README §2.5。
    t2v_provider: Literal["mock", "zhipu"] = Field(default="mock")
    zhipu_api_key: str = Field(default="")
    zhipu_base_url: str = Field(default="https://open.bigmodel.cn/api/paas/v4")
    zhipu_video_model: str = Field(default="cogvideox-3")
    # 以下两项仅在调用 cogvideox-3 时写入 REST body（cogvideox-2 不支持，强行带上会 400）。
    # .env 里通常是字符串 "30" / "5" —— pydantic-settings 不会自动把 Literal[int] 从 str 转换，
    # 故用 int + before 校验器做兼容。
    zhipu_video_fps: int = Field(default=30)
    zhipu_video_duration: int = Field(default=5)
    # 提交 / 查询 单次 HTTP 超时；视频生成本身在智谱侧异步，这里只控制 HTTP 调用本身。
    t2v_timeout_seconds: int = Field(default=30, ge=5, le=120)
    # 用户 prompt 硬上限（智谱官方 512 字符；我们留 12 字余量）。
    t2v_max_prompt_chars: int = Field(default=500, ge=20, le=512)
    # mock 模式下"假装生成时间"——让前端轮询 UI 真有进度感（默认 8s）。
    t2v_mock_duration_seconds: float = Field(default=8.0, ge=0.0, le=120.0)

    # === CORS ===
    cors_origins: str = Field(default="*")

    # === Logging ===
    log_level: str = Field(default="INFO")
    log_dir: Path = Field(default=SERVER_DIR / "logs")

    # === Static files ===
    static_root: Path = Field(default=DEFAULT_STATIC_ROOT)

    model_config = SettingsConfigDict(
        env_file=str(DEFAULT_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("zhipu_video_fps", mode="before")
    @classmethod
    def _coerce_zhipu_fps(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return value

    @field_validator("zhipu_video_duration", mode="before")
    @classmethod
    def _coerce_zhipu_duration(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return value

    @model_validator(mode="after")
    def _validate_zhipu_video_nums(self) -> "Settings":
        if self.zhipu_video_fps not in (30, 60):
            raise ValueError("ZHIPU_VIDEO_FPS must be 30 or 60")
        if self.zhipu_video_duration not in (5, 10):
            raise ValueError("ZHIPU_VIDEO_DURATION must be 5 or 10")
        return self

    @property
    def cors_origins_list(self) -> List[str]:
        """Parse comma-separated CORS origins into a list. `*` stays as `["*"]`."""
        raw = self.cors_origins.strip()
        if not raw:
            return []
        return [o.strip() for o in raw.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        """Heuristic: production usually binds 0.0.0.0 or non-default port behind nginx."""
        return self.port != 8090 or self.host == "0.0.0.0"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance. Use this everywhere instead of `Settings()` directly."""
    return Settings()
