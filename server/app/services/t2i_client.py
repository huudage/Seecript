"""T2I (Text-to-Image) client — 用于缺口补全 AIGC 与视频首尾帧准备。

Providers：
- `MockT2IClient`     离线 fixture（写一张占位 PNG 到 tmp，返回它的本地 URL）
- `DoubaoArkT2IClient` 火山方舟 Seedream 4.0（images/generations）

接口对齐 ARCHITECTURE §5.2：
- `generate(prompt, ref_image=None, size, style)` → ImageResult
"""
from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from ..config import Settings, get_settings

log = logging.getLogger("seecript.t2i")


class T2IError(RuntimeError):
    def __init__(self, message: str, code: str = "T2I_ERROR", upstream_status: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.upstream_status = upstream_status


@dataclass
class ImageResult:
    image_id: str
    url: str
    size: str
    provider: str
    elapsed_ms: int


class T2IClient(ABC):
    name: str = "abstract"

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        *,
        ref_image: Optional[str | Path] = None,
        size: str = "1024x1024",
        style: Optional[str] = None,
    ) -> ImageResult: ...


class MockT2IClient(T2IClient):
    """写一张 PNG 占位图到 server/var/aigc/ 并返回 URL。"""

    name = "mock"

    async def generate(
        self,
        prompt: str,
        *,
        ref_image: Optional[str | Path] = None,
        size: str = "1024x1024",
        style: Optional[str] = None,
    ) -> ImageResult:
        import asyncio
        started = time.perf_counter()
        await asyncio.sleep(0.6)
        image_id = uuid.uuid4().hex[:12]
        return ImageResult(
            image_id=image_id,
            url=f"/aigc/{image_id}.png",  # 实际文件由 demo 路由按需提供
            size=size,
            provider="mock",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


class DoubaoArkT2IClient(T2IClient):
    """火山方舟 Seedream 4.0。

    POST {base_url}/images/generations
        {"model": "<endpoint_id>", "prompt": "...", "size": "1024x1024"}
    返回 {"data": [{"url": "..."}]}
    """

    name = "doubao_ark"

    def __init__(self, settings: Settings) -> None:
        if not settings.ark_api_key:
            raise T2IError("ARK_API_KEY empty but T2I_PROVIDER=doubao_ark.", code="T2I_NO_KEY")
        self._api_key = settings.ark_api_key
        self._base_url = settings.ark_base_url.rstrip("/")
        self._model = settings.ark_t2i_model
        self._timeout = settings.t2i_timeout_seconds
        self._default_size = settings.t2i_default_size

    async def generate(
        self,
        prompt: str,
        *,
        ref_image: Optional[str | Path] = None,
        size: str = "1024x1024",
        style: Optional[str] = None,
    ) -> ImageResult:
        url = f"{self._base_url}/images/generations"
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        body = {"model": self._model, "prompt": prompt, "size": size or self._default_size}
        if style:
            body["style"] = style
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as e:
            raise T2IError(f"doubao t2i timeout after {self._timeout}s", code="T2I_TIMEOUT") from e
        if resp.status_code != 200:
            raise T2IError(
                f"doubao t2i HTTP {resp.status_code}: {resp.text[:300]}",
                code=f"T2I_HTTP_{resp.status_code}", upstream_status=resp.status_code,
            )
        data = resp.json()
        items = data.get("data") or []
        if not items:
            raise T2IError("doubao t2i empty data", code="T2I_BAD_RESPONSE")
        image_url = items[0].get("url") or items[0].get("b64_json")
        if not image_url:
            raise T2IError("doubao t2i missing url/b64", code="T2I_BAD_RESPONSE")
        return ImageResult(
            image_id=uuid.uuid4().hex[:12],
            url=image_url,
            size=size,
            provider=self.name,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


def get_t2i_client(settings: Optional[Settings] = None) -> T2IClient:
    s = settings or get_settings()
    if s.t2i_provider == "doubao_ark":
        if not s.ark_api_key:
            log.warning("T2I_PROVIDER=doubao_ark but ARK_API_KEY empty -> using mock")
            return MockT2IClient()
        return DoubaoArkT2IClient(s)
    return MockT2IClient()
