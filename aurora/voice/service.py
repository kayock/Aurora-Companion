"""Voice service — coordinated voice in and voice out.

The turn-taking design follows both upstreams' speech flows, adapted for
a browser client:

* Input: the browser records the user's turn (push-to-talk button or
  optional VAD in the client) and POSTs the recording — webm/opus from
  MediaRecorder, WAV from other clients — to the STT endpoint; the
  transcription goes through the normal conversation manager, so voice
  and typed chat share one history and one memory.
* Output: replies are synthesized (sentence-split when the caller wants
  chunked playback); the dashboard fetches audio from the TTS endpoint
  and plays it. Because speech hooks into the conversation manager's
  post-turn hook, *every* reply (chat, reminders, proactive) can be
  spoken, not only chat.
* Barge-in and echo cancellation depend on real audio hardware; they are
  NOT implemented in this prototype and are listed in LIMITATIONS.md.

Providers:
  TTS: none | kokoro (local server) | edge (cloud) |
       kayock (existing OpenAI-compatible speech adapter, voice ids
       discovered live from GET /v1/models)
  STT: none | whisper (local package) |
       kayock (existing Faster-Whisper transcription service)

Permissions gate everything: no microphone use unless `microphone` is
enabled, no synthesis unless `voice_output` is enabled. Voice providers
being down degrade voice only and are reported honestly via status().
"""

from __future__ import annotations

from ..permissions import PermissionRegistry
from .stt import STTProvider, WhisperSTT
from .tts import TTSProvider, NoneTTS, split_sentences


class VoiceService:
    def __init__(self, config, permissions: PermissionRegistry,
                 tts: TTSProvider | None = None, stt: STTProvider | None = None) -> None:
        self.config = config
        self.permissions = permissions
        self.tts = tts or NoneTTS()
        self.stt = stt or WhisperSTT()
        self._queue: list[tuple[str, bytes]] = []

    # -- output ------------------------------------------------------------
    async def speak(self, text: str) -> list[bytes]:
        """Synthesize (sentence-split) audio for a reply. Returns the audio
        chunks (empty when disabled/unavailable) — the server API plays or
        returns them to the client."""
        if not self.permissions.get("voice_output"):
            return []
        chunks = []
        for sentence in split_sentences(text):
            audio = await self.tts.synthesize(sentence)
            if audio:
                chunks.append(audio)
        return chunks

    async def speak_one(self, text: str, voice: str | None = None) -> tuple[bytes | None, str, str | None]:
        """One-shot synthesis for the HTTP endpoints: (audio, media_type,
        error). `voice` overrides the configured voice for this call."""
        if not self.permissions.get("voice_output"):
            return None, "audio/wav", "voice_output permission is off (Privacy panel)"
        audio = await self.tts.synthesize(text, voice=voice)
        media = getattr(self.tts, "media_type", None) or \
            ("audio/mpeg" if self.config.get("voice.format", "mp3") == "mp3" else "audio/wav")
        if not audio:
            return None, media, getattr(self.tts, "last_error", None) or \
                "speech adapter unavailable or disabled"
        return audio, media, None

    # -- voice catalog -----------------------------------------------------
    async def voices(self) -> dict:
        """Discovered voice ids + current selection (dashboard picker)."""
        selected = getattr(self.tts, "voice", None) or self.config.get("voice.voice_id", "")
        if hasattr(self.tts, "list_voices"):
            voices, err = await self.tts.list_voices()
            return {"tts": self.tts.name, "voices": voices,
                    "selected": selected, "source": "discovered" if voices else "none",
                    "error": err}
        return {"tts": self.tts.name, "voices": [], "selected": selected,
                "source": "none", "error": None}

    async def select_voice(self, voice_id: str) -> dict:
        """Persist a voice selection. When the adapter advertises its voice
        list, the id must be one of them (the adapter stays the authority —
        Aurora never guesses what is installed)."""
        voice_id = (voice_id or "").strip()
        if not voice_id:
            raise ValueError("voice id is required")
        voices, err = ([], None)
        if hasattr(self.tts, "list_voices"):
            voices, err = await self.tts.list_voices()
        if voices and voice_id not in voices:
            preview = ", ".join(voices[:12]) + ("…" if len(voices) > 12 else "")
            raise ValueError(f"voice '{voice_id}' is not offered by the speech adapter "
                             f"(available: {preview or 'none discovered'})")
        self.config.set("voice.voice_id", voice_id)
        self.config.save()
        if hasattr(self.tts, "set_voice"):
            self.tts.set_voice(voice_id)
        return {"ok": True, "voice": voice_id,
                "discovered": len(voices), "discovery_error": err}

    # -- input ---------------------------------------------------------------
    async def transcribe(self, audio: bytes, filename: str | None = None,
                         content_type: str | None = None) -> str:
        if not self.permissions.get("microphone"):
            return ""
        if getattr(self.stt, "available", None) is False:
            return ""
        kwargs: dict = {}
        if filename:
            kwargs["filename"] = filename
        if content_type:
            kwargs["content_type"] = content_type
        return await self.stt.transcribe(audio, **kwargs)

    def status(self) -> dict:
        status: dict = {
            "tts": self.tts.name,
            "stt": getattr(self.stt, "name", "none"),
            "stt_available": getattr(self.stt, "available", None),
            "voice": getattr(self.tts, "voice", None) or self.config.get("voice.voice_id", ""),
            "format": self.config.get("voice.format", "mp3"),
            "auto_speak": bool(self.config.get("voice.auto_speak", False)),
            "permissions": self.permissions.snapshot(),
        }
        if hasattr(self.tts, "endpoint"):
            status["tts_endpoint"] = self.tts.endpoint
            if hasattr(self.tts, "last_error"):
                status["tts_error"] = self.tts.last_error
        if hasattr(self.stt, "endpoint"):
            status["stt_endpoint"] = self.stt.endpoint
            if hasattr(self.stt, "last_error"):
                status["stt_error"] = self.stt.last_error
        return status

    async def close(self) -> None:
        await self.tts.close()
        await self.stt.close()
