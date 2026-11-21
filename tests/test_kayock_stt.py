"""Kayock Faster-Whisper STT integration (POST /v1/audio/transcriptions).

The service on Portable lazy-loads base.en on CPU and unloads after idle;
Aurora only forwards recordings and reports failures honestly. All HTTP
runs against httpx.MockTransport.
"""

import httpx
import pytest

from aurora.voice.kayock import KayockSTT

STT = "http://127.0.0.1:8769"  # documented local endpoint (never contacted here)


async def test_transcribe_multipart_and_text_parsing():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.content
        seen["ctype"] = request.headers.get("content-type", "")
        return httpx.Response(200, json={"text": "hello from the microphone"})

    stt = KayockSTT(STT, transport=httpx.MockTransport(handler))
    try:
        text = await stt.transcribe(b"FAKEWEBM", filename="speech.webm",
                                    content_type="audio/webm")
        assert text == "hello from the microphone"
        assert seen["path"] == "/v1/audio/transcriptions"
        assert b"speech.webm" in seen["body"]           # filename forwarded
        assert b"audio/webm" in seen["body"]            # browser format forwarded
        assert seen["ctype"].startswith("multipart/form-data")
        assert stt.available is True and stt.last_error is None
    finally:
        await stt.close()


async def test_transcribe_sends_model_field():
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("latin-1")
        return httpx.Response(200, json={"text": "ok"}) if "base.en" in body \
            else httpx.Response(400, json={"text": ""})

    stt = KayockSTT(STT, model="base.en", transport=httpx.MockTransport(handler))
    try:
        assert await stt.transcribe(b"X", filename="clip.wav") == "ok"
    finally:
        await stt.close()


async def test_transcribe_service_down_is_honest():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    stt = KayockSTT(STT, transport=httpx.MockTransport(handler))
    try:
        text = await stt.transcribe(b"FAKEWEBM", filename="speech.webm")
        assert text == ""
        assert stt.available is False
        assert stt.last_error and "transcription error" in stt.last_error
    finally:
        await stt.close()


async def test_transcribe_http_error_recorded():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="model not loaded")

    stt = KayockSTT(STT, transport=httpx.MockTransport(handler))
    try:
        assert await stt.transcribe(b"FAKEWEBM") == ""
        assert "500" in (stt.last_error or "")
    finally:
        await stt.close()


async def test_healthcheck_up_even_on_404():
    """A 404 from /models still proves the service is up (lazy loader)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    stt = KayockSTT(STT, transport=httpx.MockTransport(handler))
    try:
        assert await stt.healthcheck() is True
        assert stt.available is True
    finally:
        await stt.close()


async def test_healthcheck_down():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    stt = KayockSTT(STT, transport=httpx.MockTransport(handler))
    try:
        assert await stt.healthcheck() is False
        assert stt.available is False
    finally:
        await stt.close()


async def test_empty_audio_short_circuits():
    stt = KayockSTT(STT)
    assert await stt.transcribe(b"") == ""


async def test_alternate_json_shapes():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"transcription": "alternate shape"})

    stt = KayockSTT(STT, transport=httpx.MockTransport(handler))
    try:
        assert await stt.transcribe(b"X") == "alternate shape"
    finally:
        await stt.close()


async def test_whisper_local_provider_ignores_http_kwargs():
    """Existing local WhisperSTT keeps working when called with the new
    keyword arguments (filename/content_type)."""
    from aurora.voice.stt import NoneSTT, WhisperSTT

    stt = NoneSTT()
    assert await stt.transcribe(b"x", filename="speech.webm", content_type="audio/webm") == ""
    w = WhisperSTT()
    w.available = False  # faster-whisper not installed in this environment
    assert await w.transcribe(b"x", filename="speech.webm") == ""
