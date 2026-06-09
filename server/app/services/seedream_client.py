"""Seedream (文生图) client — 豆包方舟图像生成。

为 AIGC i2v 流程提供"参考图自动生成"能力：用户在 Compose FillAigcPanel 选 Seedream 出图后，
后端调本模块拿到 1 张图 URL，填回前端 imageSlots，最后随 reference_images 一起送给 Seedance。

Providers：
- `MockSeedreamClient`     返回 placehold.co 占位图，零依赖跑通链路。
- `DoubaoArkSeedreamClient` POST {ark_base_url}/images/generations，复用 ARK_API_KEY / ARK_T2V_API_KEY。

豆包返回的 url 是临时 CDN（1h-7d 有效）；本期不落盘，下游 Seedance.submit 立刻消费即可。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Literal, Optional
from urllib.parse import quote

import httpx

from ..config import Settings, get_settings

log = logging.getLogger("seecript.seedream")


class SeedreamError(RuntimeError):
    def __init__(self, message: str, code: str = "SEEDREAM_ERROR", upstream_status: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.upstream_status = upstream_status


@dataclass
class ImageResult:
    url: str
    width: int
    height: int
    provider: str
    elapsed_ms: int


# Seedream 5.0 强制最小 3,686,400 像素（约 2K）；以下尺寸都精确等于或略超阈值，
# 同时严格保 ratio。低版本（4.0）也兼容。
_RATIO_TO_SIZE: dict[str, tuple[int, int]] = {
    "16:9": (2560, 1440),
    "9:16": (1440, 2560),
    "1:1": (1920, 1920),
    "4:3": (2240, 1680),
    "3:4": (1680, 2240),
}


def _ratio_to_size(ratio: str) -> tuple[int, int]:
    return _RATIO_TO_SIZE.get(ratio.strip(), _RATIO_TO_SIZE["16:9"])


# 画面禁文字硬约束：Seedream 没有官方 negative_prompt 字段，但豆包对正向 prompt 末尾的
# 「不要 / 无 ...」中文模式有较好遵循度。所有走 Seedream 的 prompt 在出 HTTP 之前统一加这个尾巴。
# 字卡（text_card）走的是 Remotion CSS 渲染、不进 Seedream，因此不会被这个约束误伤。
_NO_TEXT_SUFFIX = (
    "\n\n严格要求：画面内绝对不要任何文字、汉字、字母、数字、标题、字幕、弹幕、"
    "角标、Logo、水印、印章、店招、海报上的可读字符；"
    "如果画面里出现了任何形式的文字，视为出图失败。"
)


def enforce_no_text_in_prompt(prompt: str) -> str:
    """画面禁文字硬约束。任何路径出图前都过一道，避免 Seedream 自由发挥写大字。"""
    p = (prompt or "").strip()
    if not p:
        return p
    # 已附加过则不重复（节省 token；避免重复触发 LLM 拒答）
    if "绝对不要任何文字" in p and "Logo" in p:
        return p
    return p + _NO_TEXT_SUFFIX


class SeedreamClient(ABC):
    name: str = "abstract"

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        *,
        ratio: str = "16:9",
        n: int = 1,
        watermark: bool = False,
    ) -> List[ImageResult]: ...

    async def generate_sequence(
        self,
        prompts: List[str],
        *,
        ratio: str = "16:9",
        watermark: bool = False,
    ) -> List[ImageResult]:
        """生成具有视觉一致性的多镜头序列（"故事板"模式）。

        默认实现是 fallback：把每个 prompt 单独调 generate，没有跨图一致性保证。
        DoubaoArkSeedreamClient override 这个方法，传 sequential_image_generation=auto
        让 Seedream 5.0 自己把 N 张图当作一组连续画面输出。
        """
        out: List[ImageResult] = []
        for p in prompts:
            imgs = await self.generate(p, ratio=ratio, n=1, watermark=watermark)
            if imgs:
                out.append(imgs[0])
        return out


class MockSeedreamClient(SeedreamClient):
    """占位实现：sleep 一下假装在生成，返回 placehold.co 上的纯色 PNG。"""

    name = "mock"

    def __init__(self, mock_latency_seconds: float = 1.0) -> None:
        self._latency = mock_latency_seconds

    async def generate(
        self,
        prompt: str,
        *,
        ratio: str = "16:9",
        n: int = 1,
        watermark: bool = False,
    ) -> List[ImageResult]:
        started = time.perf_counter()
        await asyncio.sleep(self._latency)
        w, h = _ratio_to_size(ratio)
        out: List[ImageResult] = []
        for i in range(max(1, n)):
            label = quote((prompt[:24] or "mock-image"), safe="")
            url = f"https://placehold.co/{w}x{h}/png?text={label}-{i+1}"
            out.append(ImageResult(
                url=url, width=w, height=h, provider=self.name,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            ))
        return out


class DoubaoArkSeedreamClient(SeedreamClient):
    """火山方舟 Seedream 4.0+ 文生图。

    POST {base_url}/images/generations
    body {
      "model": "doubao-seedream-4-0-250528",
      "prompt": "...",
      "size": "1280x720",
      "n": 1,
      "watermark": false,
      "response_format": "url"
    }
    返回 {"data": [{"url": "...", ...}, ...]}
    """

    name = "doubao_ark"

    def __init__(self, settings: Settings) -> None:
        api_key = settings.seedream_api_key  # 优先 ARK_SEEDREAM_API_KEY，否则回落 Seedance / LLM 同号 Key
        if not api_key:
            raise SeedreamError(
                "ARK_SEEDREAM_API_KEY / ARK_T2V_API_KEY / ARK_API_KEY 都为空，但 SEEDREAM_PROVIDER=doubao_ark。",
                code="SEEDREAM_NO_KEY",
            )
        self._api_key = api_key
        self._base_url = settings.ark_base_url.rstrip("/")
        self._model = settings.ark_seedream_model
        self._timeout = settings.seedream_timeout_seconds

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    async def generate(
        self,
        prompt: str,
        *,
        ratio: str = "16:9",
        n: int = 1,
        watermark: bool = False,
    ) -> List[ImageResult]:
        started = time.perf_counter()
        w, h = _ratio_to_size(ratio)
        url = f"{self._base_url}/images/generations"
        # Seedream 5.0 body 模板（match 控制台示例 curl）：
        # - sequential_image_generation=disabled：单图模式，不走"故事板序列"
        # - response_format=url：返回 CDN url（豆包 1h-7d 有效）
        # - stream=false：HTTP 一次性返回
        # - size 走精确像素 WxH（5.0 也兼容 "1K"/"2K" 预设，但我们按 ratio 算的 WxH 更可控）
        body: dict = {
            "model": self._model,
            "prompt": enforce_no_text_in_prompt(prompt)[:1500],
            "size": f"{w}x{h}",
            "sequential_image_generation": "disabled",
            "response_format": "url",
            "stream": False,
            "watermark": watermark,
        }
        # n>1 才传，单图时省略（5.0 单图模式 + n 同时传可能 400）
        if n and n > 1:
            body["n"] = max(1, min(4, n))
        # PR-L.4：豆包 Seedream 在并发 ≥3 时会偶发 ReadTimeout / 服务端 RST（httpx 抛 HTTPError），
        # 单次失败成本太高（warn fill 直接落到字卡兜底，用户看不到生成结果）。这里加 1 次重试 +
        # 指数退避，对单点 HTTPError 是几乎免费的修复，对真正的额度/参数错误（HTTP 4xx）不会重试。
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as cli:
                    resp = await cli.post(url, headers=self._headers(), json=body)
                break
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == 0:
                    log.warning("[seedream] HTTP error attempt 1/2: %r — retrying after 2s", exc)
                    await asyncio.sleep(2.0)
                    continue
                # exc.__str__() 在 ReadTimeout 等子类下可能为空——加 class 名让前端能区分超时/重置/拒绝
                detail = str(exc).strip() or exc.__class__.__name__
                raise SeedreamError(
                    f"Seedream HTTP 失败：{exc.__class__.__name__}: {detail}",
                    code="SEEDREAM_HTTP",
                ) from exc

        if resp.status_code >= 400:
            text = resp.text[:300] if resp.text else ""
            raise SeedreamError(
                f"Seedream HTTP {resp.status_code}: {text}",
                code="SEEDREAM_HTTP",
                upstream_status=resp.status_code,
            )
        data = resp.json() if resp.content else {}
        items = data.get("data") or []
        out: List[ImageResult] = []
        elapsed = int((time.perf_counter() - started) * 1000)
        for it in items:
            u = it.get("url")
            if not u:
                continue
            out.append(ImageResult(
                url=u,
                width=int(it.get("width") or w),
                height=int(it.get("height") or h),
                provider=self.name,
                elapsed_ms=elapsed,
            ))
        if not out:
            raise SeedreamError("Seedream 返回 0 张图片，请检查 prompt 或额度。", code="SEEDREAM_EMPTY")
        log.info("[seedream] generated %d image(s) in %dms (model=%s)", len(out), elapsed, self._model)
        return out

    async def generate_sequence(
        self,
        prompts: List[str],
        *,
        ratio: str = "16:9",
        watermark: bool = False,
    ) -> List[ImageResult]:
        """Seedream 5.0 故事板序列：一次调用产出 N 张视觉一致的镜头。

        关键差异：
        - sequential_image_generation=auto（不是 disabled）
        - sequential_image_generation_options.max_images=N（最多 4 张）
        - prompt：用 `\\n---\\n` 拼接 N 段 sub-prompt，Seedream 把每段当作一个镜头
        - 返回 data 是 N 张图，按提交顺序对应 N 个 prompt

        失败时由 caller 落到 default fallback（逐张 generate），不抛错以免阻塞 fill。
        """
        prompts = [p.strip() for p in prompts if p and p.strip()]
        if not prompts:
            return []
        n = max(1, min(4, len(prompts)))
        if n == 1:
            return await self.generate(prompts[0], ratio=ratio, n=1, watermark=watermark)

        started = time.perf_counter()
        w, h = _ratio_to_size(ratio)
        url = f"{self._base_url}/images/generations"
        # 用 ---SHOT-{i}--- 分隔多镜头 prompt，Seedream 5.0 会自己拆。
        joined = "\n\n".join(f"镜头{i+1}：{p}" for i, p in enumerate(prompts[:n]))
        body: dict = {
            "model": self._model,
            "prompt": enforce_no_text_in_prompt(joined)[:3000],
            "size": f"{w}x{h}",
            "sequential_image_generation": "auto",
            "sequential_image_generation_options": {"max_images": n},
            "response_format": "url",
            "stream": False,
            "watermark": watermark,
        }
        # PR-L.4：同 generate() 的重试策略——并发场景下 sequence 调用同样会偶发 HTTPError。
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as cli:
                    resp = await cli.post(url, headers=self._headers(), json=body)
                break
            except httpx.HTTPError as exc:
                if attempt == 0:
                    log.warning("[seedream] sequence HTTP error attempt 1/2: %r — retrying after 2s", exc)
                    await asyncio.sleep(2.0)
                    continue
                detail = str(exc).strip() or exc.__class__.__name__
                raise SeedreamError(
                    f"Seedream HTTP 失败：{exc.__class__.__name__}: {detail}",
                    code="SEEDREAM_HTTP",
                ) from exc

        if resp.status_code >= 400:
            text = resp.text[:300] if resp.text else ""
            raise SeedreamError(
                f"Seedream HTTP {resp.status_code}: {text}",
                code="SEEDREAM_HTTP",
                upstream_status=resp.status_code,
            )
        data = resp.json() if resp.content else {}
        items = data.get("data") or []
        out: List[ImageResult] = []
        elapsed = int((time.perf_counter() - started) * 1000)
        for it in items[:n]:
            u = it.get("url")
            if not u:
                continue
            out.append(ImageResult(
                url=u,
                width=int(it.get("width") or w),
                height=int(it.get("height") or h),
                provider=self.name,
                elapsed_ms=elapsed,
            ))
        if not out:
            raise SeedreamError("Seedream 故事板返回 0 张图。", code="SEEDREAM_EMPTY")
        log.info("[seedream] sequence generated %d/%d image(s) in %dms (model=%s)",
                 len(out), n, elapsed, self._model)
        return out


_singleton: Optional[SeedreamClient] = None


def get_seedream_client() -> SeedreamClient:
    """生产工厂：SEEDREAM_PROVIDER 必须显式 doubao_ark；其它值仅供单测。

    SEEDREAM_PROVIDER=mock 时仍允许返回 MockSeedreamClient（pytest fixture），
    但生产 .env 必须配 doubao_ark，否则 raise。
    """
    global _singleton
    if _singleton is not None:
        return _singleton
    settings = get_settings()
    if settings.seedream_provider == "doubao_ark":
        _singleton = DoubaoArkSeedreamClient(settings)
    elif settings.seedream_provider == "mock":
        _singleton = MockSeedreamClient(mock_latency_seconds=settings.seedream_mock_latency_seconds)
    else:
        raise SeedreamError(
            f"未知 SEEDREAM_PROVIDER={settings.seedream_provider!r}；生产应为 doubao_ark，单测可用 mock。",
            code="SEEDREAM_BAD_PROVIDER",
        )
    log.info("[seedream] provider=%s", _singleton.name)
    return _singleton


def reset_seedream_client() -> None:
    """For tests or hot-reload of settings."""
    global _singleton
    _singleton = None
