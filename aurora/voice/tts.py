"""Text-to-speech providers.

`none` is the default (silent). `kokoro` talks to a local kokoro-reader /
kokoro-fastapi HTTP server (the same engine Luna uses). `edge` uses the
optional `edge-tts` package (note: that one is a cloud service; Aurora
marks it clearly in the dashboard). Speech is synthesized sentence by
sentence (Shikigami's sentence splitting) so replies start playing early.
"""

from __future__ import annotations

import re

_SENTENCE_END = re.compile(r"(?<=[.!?。！？])\s+")


def split_sentences(text: str, max_chars: int = 220) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_END.split(text) if p.strip()]
    out: list[str] = []
    for part in parts:
        if len(part) <= max_chars:
            out.append(part)
            continue
        for i in range(0, len(part), max_chars):
            out.append(part[i : i + max_chars])
    return out


class TTSProvider:
    name = "base"

    async def synthesize(self, text: str, voice: str | None = None) -> bytes | None:
        """Synthesize one utterance. `voice` optionally overrides the
        configured voice for a single call (used by the voice-test API)."""
        raise NotImplementedError

    async def close(self) -> None:
        return None


class NoneTTS(TTSProvider):
    name = "none"

    async def synthesize(self, text: str) -> bytes | None:
        return None


class KokoroTTS(TTSProvider):
    """Local Kokoro server (POST OpenAI-style audio speech)."""

    name = "kokoro"

    def __init__(self, url: str, voice: str = "af_heart", timeout: float = 30.0) -> None:
        import httpx

        self.url = url.rstrip("/")
        self.voice = voice
        self._client = httpx.AsyncClient(timeout=timeout)

    async def synthesize(self, text: str, voice: str | None = None) -> bytes | None:
        try:
            resp = await self._client.post(self.url, json={
                "model": "kokoro", "input": text, "voice": voice or self.voice,
                "response_format": "wav",
            })
            resp.raise_for_status()
            return resp.content
        except Exception:
            return None

    async def close(self) -> None:
        await self._client.aclose()


class EdgeTTS(TTSProvider):
    """Optional edge-tts package (requires internet; clearly labeled)."""

    name = "edge"

    def __init__(self, voice: str = "en-US-AriaNeural") -> None:
        self.voice = voice
        self._ok: bool | None = None

    async def synthesize(self, text: str, voice: str | None = None) -> bytes | None:
        try:
            import edge_tts  # optional dependency
        except ImportError:
            self._ok = False
            return None
        try:
            communicate = edge_tts.Communicate(text, self.voice)
            buf = bytearray()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    buf.extend(chunk["data"])
            return bytes(buf) or None
        except Exception:
            return None
