"""Provider registry: build providers from config presets by role."""

from __future__ import annotations

import json
from typing import Any

from .base import ChatProvider, ChatResult, ProviderError, ToolCall, ToolSpec  # re-export
from .embeddings import EmbeddingClient
from .mock import MockProvider
from .openai_compat import OpenAICompatProvider

__all__ = [
    "ChatProvider", "ChatResult", "ProviderError", "ToolCall", "ToolSpec",
    "OpenAICompatProvider", "MockProvider", "EmbeddingClient", "create_chat_provider",
]


def create_chat_provider(preset: dict[str, Any]) -> ChatProvider:
    ptype = (preset.get("type") or "openai_compat").lower()
    if ptype == "mock":
        return MockProvider()
    if ptype == "openai_compat":
        return OpenAICompatProvider(
            base_url=preset.get("base_url", "http://127.0.0.1:1234/v1"),
            model=preset.get("model", ""),
            api_key=preset.get("api_key", ""),
            temperature=float(preset.get("temperature", 0.8)),
            max_tokens=int(preset.get("max_tokens", 1024)),
            timeout=float(preset.get("timeout", 120.0)),
        )
    raise ProviderError(f"unknown provider type: {ptype}")


def parse_tool_arguments(raw: str) -> dict:
    """Parse model tool-call arguments defensively (models often emit
    truncated or single-quoted JSON)."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except ValueError:
        try:
            value = json.loads(raw.replace("'", '"'))
            return value if isinstance(value, dict) else {}
        except ValueError:
            return {}
