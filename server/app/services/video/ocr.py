"""PaddleOCR mobile 中文 — 字幕 OCR。

PaddleOCR 自带模型下载（~10MB mobile 版），首次调用会拉模型；未安装时回落 mock。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("seecript.video.ocr")

try:
    from paddleocr import PaddleOCR
    _BACKEND = "paddleocr"
    _OCR_SINGLETON: "PaddleOCR | None" = None
except ImportError:  # pragma: no cover
    _BACKEND = "mock"
    _OCR_SINGLETON = None


@dataclass
class OCRLine:
    text: str
    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1
    confidence: float


def _get_ocr():  # pragma: no cover
    global _OCR_SINGLETON
    if _OCR_SINGLETON is None:
        _OCR_SINGLETON = PaddleOCR(use_angle_cls=False, lang="ch", show_log=False)
    return _OCR_SINGLETON


def ocr_image(image_path: str | Path) -> list[OCRLine]:
    path = Path(image_path)
    if _BACKEND == "mock" or not path.exists():
        log.warning("[ocr] backend=mock (path_exists=%s)", path.exists())
        return [OCRLine(text="[mock] 大字幕示例", bbox=(40.0, 1100.0, 680.0, 1180.0), confidence=0.95)]

    result = _get_ocr().ocr(str(path), cls=False)
    lines: list[OCRLine] = []
    if not result or not result[0]:
        return lines
    for box, (text, conf) in result[0]:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        lines.append(OCRLine(text=text, bbox=(min(xs), min(ys), max(xs), max(ys)), confidence=float(conf)))
    return lines


def backend_name() -> str:
    return _BACKEND
