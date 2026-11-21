"""Speech-to-text providers.

`none` is the default. `whisper` uses the optional faster-whisper package
(the same engine family Luna uses), running fully locally on CPU. Audio
arrives as WAV bytes from the user's client (browser microphone); the
server never opens an audio device itself.
"""

from __future__ import annotations


class STTProvider:
    name = "base"

    async def transcribe(self, audio_bytes: bytes, **kwargs) -> str:
        """Transcribe audio. Extra kwargs (filename, content_type) let
        HTTP-backed providers forward the exact browser recording format;
        local providers ignore them."""
        raise NotImplementedError

    async def close(self) -> None:
        return None


class NoneSTT(STTProvider):
    name = "none"

    async def transcribe(self, audio_bytes: bytes, **kwargs) -> str:
        return ""


class WhisperSTT(STTProvider):
    name = "whisper"

    def __init__(self, model_size: str = "small", language: str | None = None) -> None:
        self.model_size = model_size
        self.language = language
        self._model = None
        self.available: bool | None = None

    def _load(self) -> bool:
        if self._model is not None:
            return True
        try:
            from faster_whisper import WhisperModel  # optional dependency
        except ImportError:
            self.available = False
            return False
        try:
            self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8")
            self.available = True
            return True
        except Exception:
            self.available = False
            return False

    async def transcribe(self, wav_bytes: bytes, **kwargs) -> str:
        if not self._load():
            return ""
        import io
        import asyncio

        def _run() -> str:
            segments, _info = self._model.transcribe(io.BytesIO(wav_bytes), language=self.language)
            return " ".join(s.text.strip() for s in segments).strip()

        return await asyncio.to_thread(_run)
