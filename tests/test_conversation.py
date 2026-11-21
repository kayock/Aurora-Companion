"""Conversation manager: turns, tools, slash commands, events, summary."""

import json

import pytest

from aurora.providers.mock import MockProvider
from aurora.providers.base import ChatResult, ToolCall


async def test_basic_turn_persists_and_broadcasts(make_app):
    app, mock = make_app()
    events = []
    q = app.conversation.subscribe()
    import asyncio

    async def reader():
        events.append(await q.get())
    reader_task = asyncio.ensure_future(reader())
    msg = await app.conversation.submit_user_message("Hello Aurora!")
    await asyncio.sleep(0.05)
    assert msg["role"] == "assistant" and "mock" in msg["content"].lower()
    rows = app.db.query("SELECT * FROM messages ORDER BY id")
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert events and events[0]["type"] == "message"
    reader_task.cancel()


async def test_tool_loop_set_reminder(make_app):
    rules = [
        (r"\[tool set_reminder result\]", "Done!"),
        (r"remind me to check the bread",
         ChatResult(text="", finish_reason="tool_calls",
                    tool_calls=[ToolCall(name="set_reminder",
                                         arguments=json.dumps({"text": "Check the bread", "when": "in 20 minutes"}))])),
        (r"warmth", "warmth 1 energy ="),
    ]
    app, mock = make_app(rules=rules, overrides={"reminders.follow_up_default_seconds": 300})
    msg = await app.conversation.submit_user_message("Remind me to check the bread in 20 minutes")
    rems = app.reminders.list(active_only=True)
    assert len(rems) == 1
    assert rems[0].text == "Check the bread"
    assert rems[0].source == "assistant_tool"
    # the model received the tool result and produced the final text
    assert any("tool set_reminder result" in c["user"] for c in mock.calls)


async def test_provider_without_tools_skips_loop(make_app):
    app, mock = make_app()
    app.conversation.chat_provider._supports_tools = False
    msg = await app.conversation.submit_user_message("Hi without tools")
    assert msg["role"] == "assistant"


async def test_slash_command_remind(make_app):
    app, _ = make_app()
    msg = await app.conversation.submit_user_message("/remind in 5 minutes to stretch my legs")
    assert "stretch my legs" in msg["content"]
    rems = app.reminders.list(active_only=True)
    assert len(rems) == 1 and rems[0].source == "user"


async def test_slash_command_bad_time(make_app):
    app, _ = make_app()
    msg = await app.conversation.submit_user_message("/remind whenever to stretch")
    assert "Could not understand" in msg["content"]
    assert app.reminders.list(active_only=True) == []


async def test_explicit_remember_stores_fact(make_app):
    app, _ = make_app()
    msg = await app.conversation.submit_user_message(
        "Remember that my sister's name is Wren and she lives in Oslo")
    facts = app.store.active_facts()
    assert any("Wren" in f.content for f in facts)
    fact = [f for f in facts if "Wren" in f.content][0]
    assert fact.source == "user_command"
    assert fact.evidence  # linked to the user message


async def test_forget_command_tombstones(make_app):
    app, _ = make_app()
    fact = app.store.add_fact("Alex's boiler brand is Thermoking")
    msg = await app.conversation.submit_user_message("/forget Alex's boiler brand is Thermoking")
    assert not app.store.get_fact(fact.id).active
    # refusing re-adds
    from aurora.memory.store import ForgottenFact
    with pytest.raises(ForgottenFact):
        app.store.add_fact("Alex's boiler brand is Thermoking")


async def test_streaming_deltas_reach_subscribers(make_app):
    app, _ = make_app()
    q = app.conversation.subscribe()
    await app.conversation.submit_user_message("stream something")
    import asyncio
    kinds = []
    while True:
        try:
            ev = await asyncio.wait_for(q.get(), timeout=0.2)
            kinds.append(ev["type"])
        except asyncio.TimeoutError:
            break
    assert "delta" in kinds


async def test_no_model_error_is_honest(make_app):
    app, _ = make_app()
    app.conversation.chat_provider = None
    msg = await app.conversation.submit_user_message("hello?")
    assert "No chat model is configured" in msg["content"]


async def test_summary_rolls_forward(make_app):
    import asyncio
    app, _ = make_app(overrides={"conversation.summary_trigger_messages": 8,
                                 "conversation.max_history_turns": 2})
    for i in range(6):
        await app.conversation.submit_user_message(f"message number {i} for the summary test")
    await asyncio.sleep(0.3)  # post-turn hooks are fire-and-forget
    summary = app.db.get_meta("conversation.summary")
    assert summary and "Running summary" in summary


async def test_person_guest_isolation(make_app):
    app, _ = make_app(overrides={"people.multi_person": True})
    app.people.add("Casey")
    await app.conversation.submit_user_message("Remember that I love sailing", person="Casey")
    owner_facts = app.store.active_facts(subject="owner")
    casey_facts = app.store.active_facts(subject="Casey")
    assert any("sailing" in f.content for f in casey_facts)
    assert not any("sailing" in f.content for f in owner_facts)


async def test_stats_counter(make_app):
    app, _ = make_app()
    await app.conversation.submit_user_message("one")
    await app.conversation.submit_user_message("two")
    stats = app.db.get_meta_json("stats", {})
    assert stats["user_turns"] == 2


async def test_provider_error_is_honest(make_app):
    """A failing model produces an honest error message, not a crash or fake text."""
    from aurora.providers.base import ProviderError

    class Failing:
        name = "failing"

        async def chat(self, messages, tools=None, temperature=None, max_tokens=None, on_delta=None):
            raise ProviderError("simulated outage")

        def supports_tools(self):
            return False

    app, _ = make_app()
    app.conversation.chat_provider = Failing()
    msg = await app.conversation.submit_user_message("hello")
    assert "couldn't reach my language model" in msg["content"]
