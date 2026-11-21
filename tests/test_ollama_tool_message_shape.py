"""Ollama rejects Aurora's previous non-OpenAI tool_calls shape with HTTP 400."""
from __future__ import annotations

import inspect

from aurora.conversation import manager as mgr


def test_tool_loop_emits_openai_shaped_tool_calls():
    src = inspect.getsource(mgr.ConversationManager._generate_reply)
    assert '"type": "function"' in src or "'type': 'function'" in src
    assert "tool_call_id" in src
    assert '"role": "tool"' in src or "'role': 'tool'" in src
    # old shape must not be the only path
    assert '[{"name": c.name, "arguments": c.arguments}' not in src
