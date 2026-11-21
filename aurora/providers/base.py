"""Provider abstraction: the seam between Aurora and any LLM server.

A provider turns a list of chat messages (plus optional tool specs) into
assistant text, streaming deltas as they arrive. Capabilities are queried
explicitly: a provider that cannot do OpenAI-style tool calls simply
reports `supports_tools() == False` and the conversation manager skips
the tool loop (matching how many local models behave).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

DeltaCallback = Callable[[str], None]


@dataclass
class ToolCall:
    name: str
    arguments: str  # raw JSON string


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict = field(default_factory=dict)


@dataclass
class ChatResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"


ProviderError = RuntimeError


class ChatProvider(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    async def chat(
        self,
        messages: list[dict],
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_delta: DeltaCallback | None = None,
    ) -> ChatResult:
        """Run one completion. Messages are OpenAI-shaped dicts
        {role, content, tool_results?}. If on_delta is given, stream
        visible text deltas through it as they arrive."""

    def supports_tools(self) -> bool:
        return False

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None

    async def healthcheck(self) -> bool:
        return True
