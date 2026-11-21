"""Voice service and provider plumbing (capability honesty)."""

import pytest

from aurora.permissions import PermissionRegistry
from aurora.voice.service import VoiceService
from aurora.voice.stt import WhisperSTT
from aurora.voice.tts import NoneTTS, split_sentences


def test_sentence_splitting():
    parts = split_sentences("First sentence. Second one! A third; short. ")
    assert parts == ["First sentence.", "Second one!", "A third; short."]
    long = "word " * 120
    parts = split_sentences(long, max_chars=100)
    assert all(len(p) <= 110 for p in parts) and len(parts) >= 5


async def test_voice_output_disabled_by_default(make_app):
    app, _ = make_app()
    chunks = await app.voice.speak("Hello there")
    assert chunks == []  # voice_output permission off + none provider


async def test_voice_output_with_kokoro_mock(make_app, monkeypatch):
    class FakeKokoro:
        name = "kokoro"

        async def synthesize(self, text):
            return b"WAVDATA-" + text.encode()

    app, _ = make_app()
    app.voice.tts = FakeKokoro()
    app.permissions.set("voice_output", True)
    chunks = await app.voice.speak("One. Two.")
    assert len(chunks) == 2  # sentence by sentence


async def test_mic_transcription_requires_permission(make_app):
    app, _ = make_app()
    assert await app.voice.transcribe(b"wav") == ""  # permission off


async def test_whisper_absent_is_honest(make_app):
    app, _ = make_app()
    app.permissions.set("microphone", True)
    stt = WhisperSTT()
    if stt.available is False:  # faster-whisper not installed in this env
        text = await app.voice.transcribe(b"wav")
        assert text == ""
    else:
        stt._load()  # pragma: no cover


def test_permission_unknown_raises(make_app):
    app, _ = make_app()
    with pytest.raises(KeyError):
        app.permissions.set("not_a_permission", True)


def test_openai_compat_url_normalization():
    from aurora.providers.openai_compat import OpenAICompatProvider

    p = OpenAICompatProvider("http://localhost:1234/v1/chat/completions", model="m")
    assert p.base_url == "http://localhost:1234/v1"
    p2 = OpenAICompatProvider("http://localhost:8080", model="m")
    assert p2.base_url == "http://localhost:8080/v1"


def test_tool_arguments_defensive_parsing():
    from aurora.providers import parse_tool_arguments

    assert parse_tool_arguments("{}") == {}
    assert parse_tool_arguments('{"a": 1}') == {"a": 1}
    assert parse_tool_arguments("{'a': 1}") == {"a": 1}
    assert parse_tool_arguments("garbage") == {}
    assert parse_tool_arguments("") == {}


def test_registry_builds():
    from aurora.providers import create_chat_provider

    p = create_chat_provider({"type": "mock"})
    assert p.supports_tools()
    p2 = create_chat_provider({"type": "openai_compat", "base_url": "http://127.0.0.1:1/v1"})
    assert p2.supports_tools()
