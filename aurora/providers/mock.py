"""Deterministic mock provider — the offline backbone of the test suite.

Tests must not require a real model. The mock matches the last user
message against an ordered rule table (regex -> text or callable) and
supports the OpenAI tool dialect so the tool loop is exercised too. It
records every call, so tests can assert what prompts were actually sent.
"""

from __future__ import annotations

import json
import re
import time
from typing import Callable, Sequence

from .base import ChatProvider, ChatResult, ToolCall, ToolSpec

Responder = Callable[[re.Match, list[dict]], "str | ChatResult"]


class MockProvider(ChatProvider):
    name = "mock"

    def __init__(self, rules: Sequence[tuple[str, "str | Responder"]] = (),
                 default: str = "[mock provider] Aurora is running without a real language "
                                "model — configure an OpenAI-compatible endpoint to chat.",
                 supports_tools: bool = True) -> None:
        self.rules: list[tuple[re.Pattern, "str | Responder"]] = [
            (re.compile(pattern, re.S | re.I), response) for pattern, response in rules
        ]
        self.default = default
        self._supports_tools = supports_tools
        self.calls: list[dict] = []

    def supports_tools(self) -> bool:
        return self._supports_tools

    @staticmethod
    def _last_user_text(messages: list[dict]) -> str:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    return content
        return ""

    async def chat(self, messages, tools=None, temperature=None, max_tokens=None, on_delta=None) -> ChatResult:
        user_text = self._last_user_text(messages)
        self.calls.append({"ts": time.time(), "user": user_text, "n_messages": len(messages),
                           "tools": [t.name for t in (tools or [])]})
        result: "str | ChatResult" = None
        for pattern, response in self.rules:
            m = pattern.search(user_text)
            if m:
                result = response(m, messages) if callable(response) else response
                break
        if result is None:
            result = self.default
        if isinstance(result, str):
            chat_result = ChatResult(text=result)
        else:
            chat_result = result
        if on_delta is not None:
            step = 12
            for i in range(0, len(chat_result.text), step):
                on_delta(chat_result.text[i : i + step])
        return chat_result

    # helper for tests: build a ChatResult with a tool call
    @staticmethod
    def tool_call(name: str, arguments: dict) -> ChatResult:
        return ChatResult(text="", tool_calls=[ToolCall(name=name, arguments=json.dumps(arguments))],
                          finish_reason="tool_calls")
