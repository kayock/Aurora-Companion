"""Kayock Voice integration — clients for the speech infrastructure that
already runs on the Portable host.

Two independent services, both OpenAI-compatible dialects:

* TTS — the `kayock-openai-speech-adapter` (systemd service) at e.g.
  ``http://10.0.0.45:8768/v1``: ``POST /v1/audio/speech`` synthesizes
  WAV/MP3, ``GET /v1/models`` lists the voice ids it can produce
  (``am_adam``, ``picard``, ``data``, ...). Aurora discovers the real ids
  at runtime instead of hardcoding a voice table.
* STT — the Faster-Whisper service (`kayock-craft-stt.service`, lazy
  loading, auto-unload) at e.g. ``http://127.0.0.1:8769/v1``:
  ``POST /v1/audio/transcriptions`` accepts browser recordings
  (webm/opus) and returns ``{"text": ...}``.

Both clients are strictly additive: they only *call* the services and
never configure, restart, or unload anything. Every failure is recorded
in ``last_error`` and reported honestly to the dashboard; the adapters
being down degrades voice only — memory, reminders, dashboard and chat
keep working. No API key is required on the local interface, but one can
be configured.
"""

from __future__ import annotations

import mimetypes

import httpx

from .tts import TTSProvider
from .stt import STTProvider


def _normalize_base(url: str) -> str:
    """Accept 'http://host:port', 'http://host:port/v1' or a full endpoint
    paste and normalize to an OpenAI-style base ending in /v1."""
    base = (url or "").strip().rstrip("/")
    if not base:
        return ""
    for suffix in ("/audio/speech", "/audio/transcriptions", "/chat/completions"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    if not base.endswith("/v1"):
        base += "/v1"
    return base


class KayockTTS(TTSProvider):
    """OpenAI-compatible speech adapter (Kokoro / Piper / RVC behind it)."""

    name = "kayock"

    def __init__(self, base_url: str, voice: str = "am_adam", model: str = "kokoro",
                 response_format: str = "mp3", api_key: str = "", timeout: float = 45.0,
                 voice_cache_seconds: float = 300.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.endpoint = _normalize_base(base_url)
        self.voice = voice
        self.model = model
        self.format = response_format
        self._client = httpx.AsyncClient(
            base_url=self.endpoint,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=timeout,
            transport=transport,
        )
        self._voices: list[str] | None = None
        self._voices_ts: float = 0.0
        self._voices_error: str | None = None
        self._voice_cache_seconds = voice_cache_seconds
        self._last_error: str | None = None
        self._clock = None  # optional Clock injection (tests)

    # -- discovery ----------------------------------------------------------
    async def list_voices(self, force: bool = False) -> tuple[list[str], str | None]:
        """(voice_ids, error). Ids come from GET /v1/models, parsed from any
        OpenAI-ish shape: {"data":[{"id":...}]}, a bare list of strings, or
        entries with name/voice_id keys. Result is cached briefly."""
        import time as _time

        now = _time.monotonic()
        if not force and self._voices is not None and \
                now - self._voices_ts < self._voice_cache_seconds:
            return self._voices, self._voices_error
        try:
            # Prefer /voices (Kayock adapter on Portable); fall back to /models.
            data = None
            last_exc: Exception | None = None
            for path in ("/voices", "/models"):
                try:
                    resp = await self._client.get(path)
                    if resp.status_code == 404:
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except Exception as exc:
                    last_exc = exc
                    continue
            if data is None:
                raise last_exc or RuntimeError("no voice discovery endpoint")
            items = data.get("data", []) if isinstance(data, dict) else data
            if not isinstance(items, list):
                items = []
            ids: list[str] = []
            for item in items:
                if isinstance(item, str):
                    ids.append(item)
                elif isinstance(item, dict):
                    for key in ("voice", "voice_id", "id", "name"):
                        if item.get(key):
                            ids.append(str(item[key]))
                            break
            self._voices = sorted(dict.fromkeys(i for i in ids if i))
            self._voices_ts = now
            self._voices_error = None
        except Exception as exc:
            self._voices_error = f"voice discovery failed: {exc}"[:200]
            # keep a stale list if we have one; otherwise report none
            if self._voices is None:
                self._voices = []
        return self._voices, self._voices_error

    async def healthcheck(self) -> bool:
        try:
            resp = await self._client.get("/models")
            return resp.status_code < 500
        except httpx.HTTPError:
            return False

    # -- synthesis ----------------------------------------------------------
    async def synthesize(self, text: str, voice: str | None = None) -> bytes | None:
        body: dict = {"input": text,
                      "voice": voice or self.voice,
                      "response_format": self.format}
        if self.model:
            body["model"] = self.model
        try:
            resp = await self._client.post("/audio/speech", json=body)
            resp.raise_for_status()
            self._last_error = None
            return resp.content or None
        except Exception as exc:
            self._last_error = f"speech adapter error: {exc}"[:200]
            return None

    def set_voice(self, voice_id: str) -> None:
        self.voice = voice_id

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def media_type(self) -> str:
        return "audio/mpeg" if self.format == "mp3" else "audio/wav"

    async def close(self) -> None:
        await self._client.aclose()


class KayockSTT(STTProvider):
    """Faster-Whisper transcription service (lazy-loading, auto-unload)."""

    name = "kayock_whisper"

    def __init__(self, base_url: str, model: str = "base.en", language: str | None = None,
                 api_key: str = "", timeout: float = 60.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.endpoint = _normalize_base(base_url)
        self.model = model
        self.language = language
        self._client = httpx.AsyncClient(
            base_url=self.endpoint,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=timeout,
            transport=transport,
        )
        self.available: bool | None = None  # None = not probed yet
        self._last_error: str | None = None

    async def healthcheck(self) -> bool:
        """True when the STT service answers at all (404 also means up)."""
        for path in ("/models", "/health", "/"):
            try:
                resp = await self._client.get(path)
                if resp.status_code < 500:
                    self.available = True
                    self._last_error = None
                    return True
            except httpx.HTTPError:
                continue
        self.available = False
        self._last_error = "transcription service unreachable"
        return False

    async def transcribe(self, audio_bytes: bytes, filename: str = "speech.webm",
                         content_type: str | None = None) -> str:
        """Forward a browser recording (webm/opus, wav, ...) unchanged — the
        service already handles the formats existing applications use."""
        if not audio_bytes:
            return ""
        ctype = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        data = {"model": self.model} if self.model else {}
        if self.language:
            data["language"] = self.language
        try:
            resp = await self._client.post(
                "/audio/transcriptions",
                files={"file": (filename, audio_bytes, ctype)},
                data=data,
            )
            resp.raise_for_status()
            self.available = True
            self._last_error = None
            payload = resp.json()
            text = payload.get("text")
            if text is None and isinstance(payload, dict):
                text = payload.get("transcription") or payload.get("result")
            return str(text or "").strip()
        except Exception as exc:
            self.available = False
            self._last_error = f"transcription error: {exc}"[:200]
            return ""

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def close(self) -> None:
        await self._client.aclose()
