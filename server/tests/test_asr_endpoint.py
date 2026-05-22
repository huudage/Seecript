"""End-to-end test for /api/asr/transcribe (mock provider)."""
from __future__ import annotations

from io import BytesIO


def test_transcribe_returns_mock_transcript(client):
    """A small audio blob should round-trip through the mock provider."""
    fake_audio = b"\xff\xfb\x90\x00" + b"\x00" * 1024  # mp3 magic + padding
    files = {"file": ("test.mp3", BytesIO(fake_audio), "audio/mpeg")}
    r = client.post("/api/asr/transcribe", files=files)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "transcript" in body and len(body["transcript"]) > 0
    assert body["provider"] == "mock"
    assert body["elapsed_ms"] >= 0


def test_transcribe_rejects_non_audio_extension(client):
    files = {"file": ("test.txt", BytesIO(b"not an audio"), "text/plain")}
    r = client.post("/api/asr/transcribe", files=files)
    assert r.status_code == 415


def test_transcribe_rejects_empty_body(client):
    files = {"file": ("test.mp3", BytesIO(b""), "audio/mpeg")}
    r = client.post("/api/asr/transcribe", files=files)
    assert r.status_code == 400


def test_transcribe_rejects_oversize(client):
    """Build a > 25 MB blob to hit the size cap."""
    huge = b"\xff" * (26 * 1024 * 1024)
    files = {"file": ("big.mp3", BytesIO(huge), "audio/mpeg")}
    r = client.post("/api/asr/transcribe", files=files)
    assert r.status_code == 413
