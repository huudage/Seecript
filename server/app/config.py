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
    # doubao-seed-2.0-lite 是多模态模型——VLM 帧打标、段落分析、缺口判定全走它，
    # 不再保留独立 VLM/T2I 客户端。
    llm_provider: Literal["mock", "doubao_ark", "deepseek"] = Field(default="mock")
    # --- Doubao Ark (火山方舟) ---
    # base_url 走 OpenAI 兼容根路径；model 实际填 endpoint_id（如 ep-20260508213828-7ntjl）。
    ark_api_key: str = Field(default="")
    ark_base_url: str = Field(default="https://ark.cn-beijing.volces.com/api/v3")
    ark_llm_model: str = Field(default="doubao-seed-2-0-lite")
    # Seedance 2.0 fast：480p/720p、4-15s、低成本低延迟，适合 demo 高频迭代。
    # 标准版 doubao-seedance-2-0-260128 支持 1080p 但单价 + 排队耗时都更高。
    ark_t2v_model: str = Field(default="doubao-seedance-2-0-fast-260128")
    # Seedance 与 LLM 通常用同一个方舟账号；如果走独立计费 Key 单独配 ARK_T2V_API_KEY，
    # 留空时 t2v_api_key 属性自动回落到 ark_api_key。
    ark_t2v_api_key: str = Field(default="")
    # --- DeepSeek (向后兼容) ---
    deepseek_api_key: str = Field(default="")
    deepseek_base_url: str = Field(default="https://api.deepseek.com")
    deepseek_model: str = Field(default="deepseek-chat")
    # --- 共享参数 ---
    llm_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    llm_timeout_seconds: int = Field(default=120, ge=5, le=600)
    llm_max_tokens: int = Field(default=2048, ge=128, le=8192)

    # === ASR (Doubao 大模型录音文件识别 2.0 / seedasr · 异步 submit+query) ===
    # 2.0 资源 ID 是 volc.bigasr.auc（注：1.0 旧版是 volc.bigasr.auc，2.0 文档示例同名；
    # seedasr.auc 是 2.0 别名，按 .env 配置走）。1.0 极速版 (volc.bigasr.auc_turbo) 已弃用。
    # 2.0 强制 audio.url —— 不再支持 base64 inline，必须配 PUBLIC_AUDIO_BASE_URL 拼公网地址。
    # 鉴权两种模式（asr_client.py 自动识别）：
    #   - 新控制台：只设 DOUBAO_API_KEY（长串/UUID），发 X-Api-Key
    #   - 旧控制台：DOUBAO_API_KEY=AppID（数字）+ DOUBAO_ACCESS_KEY=AccessToken（长串），
    #              发 X-Api-App-Key + X-Api-Access-Key
    asr_provider: Literal["mock", "doubao"] = Field(default="mock")
    doubao_api_key: str = Field(default="")
    doubao_access_key: str = Field(default="")
    doubao_resource_id: str = Field(default="volc.bigasr.auc")
    doubao_submit_url: str = Field(
        default="https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
    )
    doubao_query_url: str = Field(
        default="https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
    )
    # 提供给火山服务端拉音频用。建议 cloudflared tunnel / ngrok / 对象存储任一。
    # 例：https://abcd-1234.trycloudflare.com（自动指向本地 8090，路径 /samples/<id>/video.mp4 直接可访问）
    public_audio_base_url: str = Field(default="")
    asr_timeout_seconds: int = Field(default=120, ge=10, le=600)
    asr_poll_interval_seconds: float = Field(default=2.0, ge=0.5, le=30.0)
    asr_poll_max_seconds: float = Field(default=300.0, ge=10.0, le=600.0)

    # === T2V（视频生成，doubao-seedance-2.0 多模态参考帧/参考视频/参考音频）===
    # 默认 mock：开箱即用；切到 doubao_ark 需在 .env 设 ARK_API_KEY 或独立 ARK_T2V_API_KEY。
    # Seedance 2.0 用 ratio 而不是 size；duration 受模型最低时长约束（5s 起，3s 会被拒）。
    t2v_provider: Literal["mock", "doubao_ark"] = Field(default="mock")
    # T2V HTTP 单次调用窗口：Seedance submit 通常 1-5s 返回 task_id，
    # query 也是亚秒级；但偶尔会有 30s+ 的尖刺。给到 60s 是为这些尖刺兜底，
    # 真正的"等任务跑完"由 _generate_chunks 内部 max_wait（默认 180s）管。
    t2v_timeout_seconds: int = Field(default=60, ge=5, le=300)
    t2v_max_prompt_chars: int = Field(default=500, ge=20, le=512)
    # mock 模式下"假装生成时间"——让前端轮询 UI 真有进度感（默认 8s）。
    t2v_mock_duration_seconds: float = Field(default=8.0, ge=0.0, le=120.0)
    # 画幅与音频开关给 gap_agent / seedance_chain 提供默认值。
    t2v_default_ratio: str = Field(default="16:9")
    t2v_generate_audio: bool = Field(default=False)
    t2v_watermark: bool = Field(default=False)

    # === TTS（口播合成，火山方舟 TTS HTTP）===
    # 默认 mock：无 Key 时合成单调正弦波 wav（足够 demo 链路跑通，文案能写进音频流）；
    # 切到 volc 需在 .env 设 VOLC_TTS_APP_ID + VOLC_TTS_ACCESS_TOKEN（独立鉴权，
    # 不复用 ARK_API_KEY；如果未来切到方舟 OpenAI 兼容端点，再加 tts_api_key）。
    tts_provider: Literal["mock", "volc"] = Field(default="mock")
    volc_tts_app_id: str = Field(default="")
    volc_tts_access_token: str = Field(default="")
    volc_tts_endpoint: str = Field(
        default="https://openspeech.bytedance.com/api/v1/tts"
    )
    volc_tts_cluster: str = Field(default="volcano_tts")
    tts_default_voice: str = Field(default="zh_female_qingxin")
    tts_sample_rate: int = Field(default=24000, ge=8000, le=48000)
    tts_timeout_seconds: int = Field(default=30, ge=5, le=120)

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

    @field_validator("t2v_provider", mode="before")
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
    def t2v_api_key(self) -> str:
        """Effective Seedance Key — 独立 ARK_T2V_API_KEY 优先，未配置回落到 ARK_API_KEY。"""
        return self.ark_t2v_api_key or self.ark_api_key

    @property
    def is_production(self) -> bool:
        """Heuristic: production usually binds 0.0.0.0 or non-default port behind nginx."""
        return self.port != 8090 or self.host == "0.0.0.0"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance. Use this everywhere instead of `Settings()` directly."""
    return Settings()
