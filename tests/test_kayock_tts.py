"""Kayock speech-adapter compatibility (OpenAI /v1/audio/speech dialect).

All adapter traffic runs against httpx.MockTransport — no LAN access is
needed or attempted in the sandbox. Voice ids are discovered from
/v1/models exactly as Aurora does on Portable; nothing is hardcoded.
"""

import json

import httpx
import pytest

from aurora.voice.kayock import KayockTTS, _normalize_base

ADAPTER = "http://10.0.0.45:8768"  # the documented Portable address (never contacted here)


def test_base_url_normalization():
    assert _normalize_base("http://10.0.0.45:8768") == "http://10.0.0.45:8768/v1"
    assert _normalize_base("http://10.0.0.45:8768/v1/") == "http://10.0.0.45:8768/v1"
    assert _normalize_base("http://10.0.0.45:8768/v1/audio/speech") == "http://10.0.0.45:8768/v1"
    assert _normalize_base("") == ""


async def test_voice_discovery_openai_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"object": "list", "data": [
            {"id": "am_adam"}, {"id": "picard"}, {"id": "data"}, {"id": "eric"}]})

    tts = KayockTTS(ADAPTER, transport=httpx.MockTransport(handler))
    try:
        voices, err = await tts.list_voices()
        assert voices == sorted(["am_adam", "picard", "data", "eric"])
        assert err is None
    finally:
        await tts.close()


async def test_voice_discovery_bare_string_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["am_adam", "eric", "optimus_prime"])

    tts = KayockTTS(ADAPTER, transport=httpx.MockTransport(handler))
    try:
        voices, err = await tts.list_voices()
        assert voices == ["am_adam", "eric", "optimus_prime"] and err is None
    finally:
        await tts.close()


async def test_voice_discovery_cached_until_forced():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"data": [{"id": "am_adam"}]})

    tts = KayockTTS(ADAPTER, transport=httpx.MockTransport(handler))
    try:
        await tts.list_voices()
        await tts.list_voices()          # served from cache
        assert calls["n"] == 1
        await tts.list_voices(force=True)  # refresh goes to the wire
        assert calls["n"] == 2
    finally:
        await tts.close()


async def test_discovery_failure_keeps_stale_list_and_reports():
    state = {"down": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["down"]:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"data": [{"id": "picard"}]})

    tts = KayockTTS(ADAPTER, transport=httpx.MockTransport(handler))
    try:
        voices, err = await tts.list_voices()
        assert voices == ["picard"]
        state["down"] = True
        voices2, err2 = await tts.list_voices(force=True)
        assert voices2 == ["picard"]          # stale cache retained
        assert err2 and "discovery failed" in err2
    finally:
        await tts.close()


async def test_synthesize_request_shape_and_audio():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"MP3-BYTES", headers={"content-type": "audio/mpeg"})

    tts = KayockTTS(ADAPTER, voice="am_adam", transport=httpx.MockTransport(handler))
    try:
        audio = await tts.synthesize("Aurora speaking.", voice="picard")
        assert audio == b"MP3-BYTES"
        assert seen["path"] == "/v1/audio/speech"
        assert seen["body"]["voice"] == "picard"      # per-call override honored
        assert seen["body"]["input"] == "Aurora speaking."
        assert seen["body"]["response_format"] == "mp3"
        assert tts.last_error is None
        assert tts.media_type == "audio/mpeg"
    finally:
        await tts.close()


async def test_synthesize_server_error_recorded_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="RVC busy")

    tts = KayockTTS(ADAPTER, transport=httpx.MockTransport(handler))
    try:
        assert await tts.synthesize("hello") is None
        assert tts.last_error and "503" in tts.last_error
    finally:
        await tts.close()


async def test_synthesize_connection_refused_recorded():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    tts = KayockTTS(ADAPTER, transport=httpx.MockTransport(handler))
    try:
        assert await tts.synthesize("hello") is None
        assert tts.last_error and "refused" in tts.last_error
        assert await tts.healthcheck() is False
    finally:
        await tts.close()


async def test_healthcheck_up():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "am_adam"}]})

    tts = KayockTTS(ADAPTER, transport=httpx.MockTransport(handler))
    try:
        assert await tts.healthcheck() is True
    finally:
        await tts.close()


async def test_media_type_wav():
    tts = KayockTTS(ADAPTER, response_format="wav")
    assert tts.media_type == "audio/wav"


async def test_set_voice_updates_target():
    tts = KayockTTS(ADAPTER, voice="am_adam")
    tts.set_voice("darth_vader")
    assert tts.voice == "darth_vader"
