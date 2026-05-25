"""VLM (Vision-Language Model) client abstraction.

用途：
- 拆解 Agent · 关键帧打标（封面风格 / 转场类型 / 字幕样式 / 物体场景）
- 新素材 · VLM 标签 + 段落推荐

Providers：
- `MockVLMClient`     离线 fixture
- `DoubaoArkVLMClient` 火山方舟 Doubao-Seed-1.6-vision（chat/completions + image_url）
"""
from __future__ import annotations

import base64
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import httpx

from ..config import Settings, get_settings

log = logging.getLogger("seecript.vlm")


class VLMError(RuntimeError):
    def __init__(self, message: str, code: str = "VLM_ERROR", upstream_status: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.upstream_status = upstream_status


class VLMClient(ABC):
    name: str = "abstract"

    @abstractmethod
    async def tag_frames(self, images: list[str | Path], taxonomy: list[str]) -> list[dict]:
        """对一批关键帧打标。

        Returns 与 images 等长的 list，每项: {"frame": str, "tags": list[str], "subtitle_style": str}
        """

    @abstractmethod
    async def describe(self, image: str | Path, hint: str = "") -> str:
        """对单张图返回一段自然语言描述。"""


class MockVLMClient(VLMClient):
    name = "mock"

    async def tag_frames(self, images: list[str | Path], taxonomy: list[str]) -> list[dict]:
        import asyncio
        await asyncio.sleep(0.2)
        return [
            {
                "frame": str(img),
                "tags": ["[mock] 室内", "[mock] 近景", "[mock] 口播"] if i % 2 == 0
                else ["[mock] 产品特写", "[mock] 白色桌面"],
                "subtitle_style": "大字加描边" if i % 3 == 0 else "无字幕",
            }
            for i, img in enumerate(images)
        ]

    async def describe(self, image: str | Path, hint: str = "") -> str:
        return f"[mock] 一张关于 {hint or '示例画面'} 的图片"


class DoubaoArkVLMClient(VLMClient):
    """火山方舟 vision chat completions。

    payload 形如：
      messages=[{"role":"user","content":[
        {"type":"text","text":"..."},
        {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}},
      ]}]
    """

    name = "doubao_ark"

    def __init__(self, settings: Settings) -> None:
        if not settings.ark_api_key:
            raise VLMError("ARK_API_KEY is empty but VLM_PROVIDER=doubao_ark.", code="VLM_NO_KEY")
        self._api_key = settings.ark_api_key
        self._base_url = settings.ark_base_url.rstrip("/")
        self._model = settings.ark_vlm_model
        self._timeout = settings.vlm_timeout_seconds

    @staticmethod
    def _image_to_data_url(image_path: str | Path) -> str:
        p = Path(image_path)
        if not p.exists():
            raise VLMError(f"image not found: {p}", code="VLM_NO_IMAGE")
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        ext = p.suffix.lower().lstrip(".") or "jpeg"
        if ext == "jpg":
            ext = "jpeg"
        return f"data:image/{ext};base64,{b64}"

    async def _chat(self, content: list[dict]) -> str:
        url = f"{self._base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        body = {"model": self._model, "messages": [{"role": "user", "content": content}]}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as e:
            raise VLMError(f"doubao vlm timeout after {self._timeout}s", code="VLM_TIMEOUT") from e
        if resp.status_code != 200:
            raise VLMError(
                f"doubao vlm HTTP {resp.status_code}: {resp.text[:300]}",
                code=f"VLM_HTTP_{resp.status_code}", upstream_status=resp.status_code,
            )
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise VLMError("doubao vlm empty choices", code="VLM_BAD_RESPONSE")
        return choices[0].get("message", {}).get("content") or ""

    async def tag_frames(self, images: list[str | Path], taxonomy: list[str]) -> list[dict]:
        results: list[dict] = []
        tax_str = "、".join(taxonomy) if taxonomy else "封面风格、转场类型、字幕样式、物体场景"
        for img in images:
            data_url = self._image_to_data_url(img)
            content = [
                {"type": "text",
                 "text": f"请按 {tax_str} 维度列出 3-5 个标签，"
                         "并判定字幕样式（大字加描边 / 小字白底 / 无字幕）。返回 JSON："
                         '{"tags": [...], "subtitle_style": "..."}'},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]
            text = await self._chat(content)
            try:
                import json as _json
                parsed = _json.loads(text.strip().lstrip("`").rstrip("`"))
                results.append({
                    "frame": str(img),
                    "tags": parsed.get("tags", []),
                    "subtitle_style": parsed.get("subtitle_style", ""),
                })
            except Exception:
                results.append({"frame": str(img), "tags": [text[:80]], "subtitle_style": ""})
        return results

    async def describe(self, image: str | Path, hint: str = "") -> str:
        data_url = self._image_to_data_url(image)
        content = [
            {"type": "text", "text": (hint or "请用一句话描述这张图。") + " 用中文，不超过 60 字。"},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
        return await self._chat(content)


def get_vlm_client(settings: Optional[Settings] = None) -> VLMClient:
    s = settings or get_settings()
    if s.vlm_provider == "doubao_ark":
        if not s.ark_api_key:
            log.warning("VLM_PROVIDER=doubao_ark but ARK_API_KEY empty -> using mock")
            return MockVLMClient()
        return DoubaoArkVLMClient(s)
    return MockVLMClient()
