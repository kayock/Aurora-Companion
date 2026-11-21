"""Voice selection: discovery API, selection API, persistence across
restart, and honest errors — all against a mocked speech adapter."""

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from aurora.app import CompanionApp
from aurora.clock import Clock
from aurora.config import Config
from aurora.server.app import create_server
from aurora.voice.kayock import KayockSTT, KayockTTS
from tests.conftest import SEED, T0, mock_provider, standard_rules

ADAPTER = "http://10.0.0.45:8768"
VOICES = sorted(["am_adam", "batman", "data", "darth_vader", "eric",
                 "optimus_prime", "picard"])


def _adapter_tts(voice: str = "am_adam", down: bool = False) -> KayockTTS:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            if down:
                raise httpx.ConnectError("connection refused")
            return httpx.Response(200, json={"data": [{"id": v} for v in VOICES]})
        return httpx.Response(200, content=b"MP3AUDIO",
                              headers={"content-type": "audio/mpeg"})

    return KayockTTS(ADAPTER, voice=voice, transport=httpx.MockTransport(handler))


def _adapter_stt(capture: dict | None = None) -> KayockSTT:
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["body"] = request.content
        return httpx.Response(200, json={"text": "voice check transcript"})

    return KayockSTT("http://127.0.0.1:8769", transport=httpx.MockTransport(handler))


def _build(tmp_path, extra: dict | None = None) -> CompanionApp:
    provider = mock_provider(standard_rules())
    voice_cfg = {"tts": "kayock", "stt": "kayock", "kayock_tts_url": ADAPTER}
    data = {"data_dir": str(tmp_path / "data"), "voice": voice_cfg}
    if extra:
        data.update(extra)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    app = CompanionApp(Config.load(path), clock=Clock(T0),
                       seed_path=SEED, chat_provider=provider,
                       background_provider=provider)
    app.voice.tts = _adapter_tts()
    app.voice.stt = _adapter_stt()
    return app


def _config_file(tmp_path, extra: dict | None = None) -> Config:
    data = {"data_dir": str(tmp_path / "data"),
            "voice": {"tts": "kayock", "stt": "kayock"}}
    if extra:
        data.update(extra)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return Config.load(path)


def _client(app) -> TestClient:
    return TestClient(create_server(app))


def test_voices_endpoint_lists_discovered_ids(tmp_path):
    c = _client(_build(tmp_path))
    body = c.get("/api/voice/voices").json()
    assert body["tts"] == "kayock"
    assert body["voices"] == VOICES
    assert body["selected"] == "am_adam"
    assert body["source"] == "discovered"
    assert body["error"] is None


def test_select_voice_updates_live_instance_and_config(tmp_path):
    app = _build(tmp_path)
    c = _client(app)
    r = c.post("/api/voice/voice", json={"voice": "picard"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert app.voice.tts.voice == "picard"
    assert app.config.get("voice.voice_id") == "picard"
    assert (tmp_path / "config.yaml").exists()  # persisted to disk


def test_select_unknown_voice_rejected_with_available_list(tmp_path):
    c = _client(_build(tmp_path))
    r = c.post("/api/voice/voice", json={"voice": "gandalf"})
    assert r.status_code == 400
    assert "gandalf" in r.json()["detail"] and "picard" in r.json()["detail"]


def test_selection_accepted_when_discovery_down(tmp_path):
    """Adapter unreachable at selection time: Aurora still stores the
    preference (the adapter is the authority once it is back)."""
    app = _build(tmp_path)
    app.voice.tts = _adapter_tts(down=True)
    c = _client(app)
    r = c.post("/api/voice/voice", json={"voice": "eric"})
    assert r.status_code == 200
    assert app.config.get("voice.voice_id") == "eric"
    voices = c.get("/api/voice/voices").json()
    assert voices["voices"] == [] and voices["source"] == "none"
    assert "discovery failed" in (voices["error"] or "")


def test_voice_selection_survives_restart(tmp_path):
    app = _build(tmp_path)
    c = _client(app)
    assert c.post("/api/voice/voice", json={"voice": "optimus_prime"}).status_code == 200
    app.db.close()

    # rebuild from the same config file = the restart; the real KayockTTS
    # is constructed by CompanionApp from config (no replacement this time)
    provider = mock_provider(standard_rules())
    app2 = CompanionApp(Config.load(tmp_path / "config.yaml"), clock=Clock(T0),
                        seed_path=SEED, chat_provider=provider,
                        background_provider=provider)
    try:
        assert app2.voice.tts.name == "kayock"
        assert app2.voice.tts.voice == "optimus_prime"
        assert app2.voice.tts.endpoint == ADAPTER + "/v1"
    finally:
        app2.db.close()


def test_voice_test_endpoint_streams_audio(tmp_path):
    c = _client(_build(tmp_path))
    r = c.post("/api/voice/test", json={"voice": "batman", "text": "check"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.content == b"MP3AUDIO"


def test_tts_endpoint_media_type(tmp_path):
    c = _client(_build(tmp_path))
    r = c.get("/api/voice/tts", params={"text": "hello there"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"


def test_stt_endpoint_returns_transcript_and_forwards_webm(tmp_path):
    capture: dict = {}
    app = _build(tmp_path)
    app.voice.stt = _adapter_stt(capture)
    c = _client(app)
    r = c.post("/api/voice/stt", files={"file": ("speech.webm", b"OPUSDATA", "audio/webm")})
    assert r.status_code == 200
    assert r.json()["text"] == "voice check transcript"
    assert b"speech.webm" in capture["body"] and b"audio/webm" in capture["body"]


def test_voice_status_reports_endpoints(tmp_path):
    c = _client(_build(tmp_path))
    status = c.get("/api/voice/status").json()
    assert status["tts"] == "kayock" and status["stt"] == "kayock_whisper"
    assert status["tts_endpoint"] == ADAPTER + "/v1"
    assert status["stt_endpoint"] == "http://127.0.0.1:8769/v1"
    assert status["voice"] == "am_adam"
    assert status["permissions"]["microphone"] is True   # stt configured
    assert status["permissions"]["voice_output"] is True  # tts configured
