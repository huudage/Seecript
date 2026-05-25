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

from pydantic import Field, field_validator
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
    # 默认 mock（不依赖任何 Key）；接到火山方舟时切到 `doubao_ark`，旧的 deepseek 仍保留。
    llm_provider: Literal["mock", "doubao_ark", "deepseek"] = Field(default="mock")
    # --- Doubao Ark (火山方舟) ---
    # base_url 走 OpenAI 兼容根路径；model 实际填 endpoint_id（如 ep-20260508213828-7ntjl）。
    ark_api_key: str = Field(default="")
    ark_base_url: str = Field(default="https://ark.cn-beijing.volces.com/api/v3")
    ark_llm_model: str = Field(default="ep-doubao-seed-2.0-lite")
    ark_vlm_model: str = Field(default="ep-doubao-seed-1.6-vision")
    ark_t2i_model: str = Field(default="ep-doubao-seedream-4.0")
    ark_t2v_model: str = Field(default="ep-doubao-seedance-1.0-pro")
    # --- DeepSeek (向后兼容) ---
    deepseek_api_key: str = Field(default="")
    deepseek_base_url: str = Field(default="https://api.deepseek.com")
    deepseek_model: str = Field(default="deepseek-chat")
    # --- 共享参数 ---
    llm_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    llm_timeout_seconds: int = Field(default=60, ge=5, le=300)
    llm_max_tokens: int = Field(default=2048, ge=128, le=8192)

    # === VLM (视频/图像理解) ===
    vlm_provider: Literal["mock", "doubao_ark"] = Field(default="mock")
    vlm_timeout_seconds: int = Field(default=60, ge=5, le=300)

    # === T2I (文生图) ===
    t2i_provider: Literal["mock", "doubao_ark"] = Field(default="mock")
    t2i_timeout_seconds: int = Field(default=60, ge=5, le=300)
    t2i_default_size: str = Field(default="1024x1024")

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

    # === T2V（视频生成，doubao-seedance-1.0-pro 首尾帧模式）===
    # 默认 mock：开箱即用；切到 doubao_ark 需在 .env 设 ARK_API_KEY。
    t2v_provider: Literal["mock", "doubao_ark"] = Field(default="mock")
    t2v_timeout_seconds: int = Field(default=30, ge=5, le=120)
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

    @field_validator("vlm_provider", "t2i_provider", "t2v_provider", mode="before")
    @classmethod
    def _normalize_provider(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

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
