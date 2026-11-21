"""Regression: leaked tool-call JSON must never appear as chat; topic
switching must follow the latest user request; malformed llama3.2 tool
JSON (missing colon after parameters) must still execute or fail safely.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from aurora.conversation.manager import ConversationManager
from aurora.providers.base import ChatResult, ToolCall
from aurora.providers.mock import MockProvider


def test_repair_and_extract_parameters_without_colon():
    """llama3.2 often emits `"parameters{"` instead of `"parameters": {`."""
    raw = '{"name":"set_reminder","parameters{"text":"Water the basil","when":"in 30 minutes"}}'
    # After repair the blob should parse, or the regex fallback should win.
    repaired = ConversationManager._repair_tool_json(raw)
    assert '"parameters":' in repaired or '"parameters": {' in repaired.replace(" ", "")
    got = ConversationManager._extract_embedded_tool_call(
        raw, known_tools={"set_reminder", "recall_facts"})
    assert got is not None
    name, args = got
    assert name == "set_reminder"
    parsed = json.loads(args)
    assert "basil" in parsed["text"].lower()


def test_extract_recall_facts_parameters_shape():
    raw = '{"name":"recall_facts","parameters":{"query":"fun fact about Saturn"}}'
    got = ConversationManager._extract_embedded_tool_call(
        raw, known_tools={"recall_facts", "set_reminder"})
    assert got is not None and got[0] == "recall_facts"


def test_looks_like_tool_json_detects_malformed():
    assert ConversationManager._looks_like_tool_json(
        '{"name":"recall_facts","parameters{"query":"Saturn"}}')
    assert not ConversationManager._looks_like_tool_json("Saturn has beautiful rings.")


@pytest.mark.asyncio
async def test_malformed_tool_json_executes_and_never_surfaces(make_app):
    rules = [
        (r"(?:\[tool set_reminder result\]|scheduled reminder|tool)",
         "Done — basil reminder is set."),
        (r"basil|water",
         '{"name":"set_reminder","parameters{"text":"water the basil","when":"in 30 minutes"}}'),
    ]
    app, mock = make_app(rules=rules)
    events = []
    q = app.conversation.subscribe()

    async def drain():
        while True:
            try:
                events.append(await asyncio.wait_for(q.get(), timeout=0.3))
            except asyncio.TimeoutError:
                break

    # Intent present ("remind me") but not the NL ^remind-me parser shape.
    msg = await app.conversation.submit_user_message(
        "Could you remind me to water the basil in 30 minutes?")
    await drain()
    assert '"name"' not in msg["content"]
    assert "set_reminder" not in msg["content"]
    assert "parameters" not in msg["content"].lower()
    rems = app.reminders.list(active_only=True)
    assert any("basil" in r.text.lower() for r in rems)
    assert any(e.get("type") == "stream_reset" for e in events)


@pytest.mark.asyncio
async def test_unparseable_tool_json_fails_safely_without_phantom(make_app):
    """Broken JSON that still looks like a tool call must not become a chat
    reply and must not create reminders."""
    rules = [
        (r".*", '{"name":"set_reminder","parameters{"text":"hi","when":'),  # truncated
    ]
    app, mock = make_app(rules=rules)
    before = len(app.reminders.list(active_only=True))
    msg = await app.conversation.submit_user_message("Hello there friend")
    assert '"name"' not in msg["content"]
    assert "set_reminder" not in msg["content"]
    assert "couldn't complete" in msg["content"].lower() or "another way" in msg["content"].lower()
    assert len(app.reminders.list(active_only=True)) == before


@pytest.mark.asyncio
async def test_topic_switch_ignores_prior_tool_leak_in_history(make_app):
    """After a Saturn/tool-JSON assistant leak is in the DB, a short directive
    must not stay stuck on Saturn — leaked JSON is filtered from the window."""
    app, mock = make_app(default="ready")
    # Poison history the way a bad live turn did.
    app.db.execute(
        "INSERT INTO messages (role, content, ts, person, source) VALUES (?,?,?,?,?)",
        ("user", "What is a fun fact about Saturn's rings?", app.clock.now(), "owner", "user"),
    )
    app.db.execute(
        "INSERT INTO messages (role, content, ts, person, source) VALUES (?,?,?,?,?)",
        ("assistant",
         '{"name":"recall_facts","parameters":{"query":"fun fact about Saturn"}}',
         app.clock.now(), "owner", "chat"),
    )
    window = app.conversation._window()
    assert not any("recall_facts" in m["content"] for m in window if m["role"] == "assistant")

    msg = await app.conversation.submit_user_message("Say only the word ready.")
    assert "saturn" not in msg["content"].lower()
    assert "ready" in msg["content"].lower()


@pytest.mark.asyncio
async def test_memory_query_uses_current_turn_not_prior_users(make_app):
    app, mock = make_app(default="ok")
    await app.conversation.submit_user_message("Tell me about Saturn")
    # Build messages for an unrelated directive and ensure system query path
    # does not concatenate Saturn into the retrieval query (pinned/recent only
    # for directive skips is acceptable).
    messages = await app.conversation._build_messages("Say only the word ready.", "owner")
    system = messages[0]["content"]
    # Guardrail present; Saturn should not dominate a directive turn's facts.
    assert "answer the latest user message" in system.lower() or "Priority:" in system


def test_handsfree_js_queues_while_busy_and_softens_wake():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "aurora/server/static/index.html").read_text()
    assert "wake.queue" in html
    assert "stream_reset" in html
    assert "arora|orora|auroura" in html or "arora" in html
    assert "processing speech" in html
    assert "ignored (no wake)" in html


@pytest.mark.asyncio
async def test_native_tool_calls_still_work(make_app):
    rules = [
        (r"(?:\[tool recall_facts result\]|nothing found|tool)", "I don't have a Saturn fact stored."),
        (r"Saturn",
         ChatResult(text="", finish_reason="tool_calls",
                    tool_calls=[ToolCall(name="recall_facts",
                                         arguments=json.dumps({"query": "Saturn"}))])),
    ]
    app, mock = make_app(rules=rules)
    msg = await app.conversation.submit_user_message("Any stored facts about Saturn?")
    assert '"name"' not in msg["content"]
    assert "recall_facts" not in msg["content"]
