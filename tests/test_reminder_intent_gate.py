"""Unauthorized set_reminder must never create persistent reminders.

Covers the 1.0.3 live failure: greetings caused the model to emit
set_reminder for 'water the basil' from prior context, and the tool
path executed it.
"""

from __future__ import annotations

import json

import pytest

from aurora.app import CompanionApp, assert_data_dir_safe_for_tests
from aurora.clock import Clock
from aurora.config import Config
from aurora.providers.base import ChatResult, ToolCall
from aurora.reminders.intent import has_scheduling_intent
from pathlib import Path


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Hi, Aurora.", False),
        ("Good morning.", False),
        ("How are you?", False),
        ("Let's discuss Saturn.", False),
        ("Tell me about growing basil.", False),
        ("I don't have any plants.", False),
        ('He said "remind me tomorrow" and left.', False),
        ("Remind me to check the servers tomorrow.", True),
        ("Set a timer for five minutes.", True),
        ("Remind me every Monday at nine to check servers.", True),
        ("Please remind me in 20 minutes.", True),
        ("Could you remind me to water the basil in 30 minutes?", True),
        ("Don't forget to call Mom at 5.", True),
        ("What reminders do I have?", False),
        ("Don't remind me about that.", False),  # cancel/discuss, not create
    ],
)
def test_has_scheduling_intent_cases(text, expected):
    assert has_scheduling_intent(text) is expected


@pytest.mark.asyncio
async def test_greeting_cannot_create_basil_reminder(make_app):
    """Exact 1.0.3 failure mode: greeting → set_reminder(water the basil)."""
    rules = [
        (r"(?:cannot schedule|error:)", "Hello! How can I help?"),
        (r"hi|hello|morning|hey",
         '{"name":"set_reminder","parameters":{"text":"water the basil","when":"in 30 minutes"}}'),
    ]
    app, mock = make_app(rules=rules)
    for msg in ("Hi, Aurora.", "Good morning.", "Hey Aurora", "How are you?"):
        before = len(app.reminders.list(active_only=True))
        reply = await app.conversation.submit_user_message(msg)
        after = app.reminders.list(active_only=True)
        assert len(after) == before, (msg, [(r.text, r.source) for r in after])
        assert "set_reminder" not in (reply.get("content") or "")
        assert '"name"' not in (reply.get("content") or "")


@pytest.mark.asyncio
async def test_topic_discussion_does_not_schedule(make_app):
    rules = [
        (r"(?:cannot schedule|error:)", "Sure — basil likes bright light."),
        (r"basil|saturn|plants",
         ChatResult(text="", finish_reason="tool_calls",
                    tool_calls=[ToolCall(name="set_reminder",
                                         arguments=json.dumps({
                                             "text": "water the basil",
                                             "when": "in 30 minutes"}))])),
    ]
    app, _ = make_app(rules=rules)
    for msg in ("Let's discuss Saturn.", "Tell me about growing basil.",
                "I don't have any plants."):
        await app.conversation.submit_user_message(msg)
    assert app.reminders.list(active_only=True) == []


@pytest.mark.asyncio
async def test_authorized_tool_reminder_still_works(make_app):
    rules = [
        (r"(?:scheduled reminder|cannot schedule)", "Done — servers reminder set."),
        (r"servers|remind",
         ChatResult(text="", finish_reason="tool_calls",
                    tool_calls=[ToolCall(name="set_reminder",
                                         arguments=json.dumps({
                                             "text": "check the servers",
                                             "when": "tomorrow"}))])),
    ]
    app, _ = make_app(rules=rules)
    # Avoid NL fast-path (^remind me…) so the tool path is exercised.
    await app.conversation.submit_user_message(
        "Tomorrow morning, please remind me to check the servers")
    rems = app.reminders.list(active_only=True)
    assert len(rems) == 1 and "servers" in rems[0].text.lower()


@pytest.mark.asyncio
async def test_nl_and_timer_still_work(make_app):
    app, mock = make_app(rules=[])
    await app.conversation.submit_user_message("Remind me to check the servers tomorrow")
    await app.conversation.submit_user_message("Set a timer for 5 minutes")
    active = app.reminders.list(active_only=True)
    assert any("servers" in r.text.lower() for r in active)
    assert any(r.kind == "timer" for r in active)
    assert mock.calls == []  # deterministic paths


@pytest.mark.asyncio
async def test_malformed_unauthorized_json_no_side_effects(make_app):
    rules = [
        (r".*", '{"name":"set_reminder","parameters{"text":"water the basil","when":"in 30 minutes"}}'),
    ]
    app, _ = make_app(rules=rules)
    reply = await app.conversation.submit_user_message("Good morning.")
    assert app.reminders.list(active_only=True) == []
    assert '"name"' not in (reply.get("content") or "")


def test_pytest_refuses_production_datadir(tmp_path):
    # Direct guard
    with pytest.raises(RuntimeError, match="production data_dir"):
        assert_data_dir_safe_for_tests(Path("/var/lib/aurora"))
    # CompanionApp construction under pytest
    cfg = Config({"data_dir": "/var/lib/aurora"})
    with pytest.raises(RuntimeError, match="production data_dir"):
        CompanionApp(cfg, clock=Clock(0.0))


def test_make_test_config_never_points_at_production(tmp_path):
    from tests.conftest import make_test_config
    cfg = make_test_config(tmp_path)
    assert "/var/lib/aurora" not in str(Path(cfg.data_dir()).resolve())
    with pytest.raises(RuntimeError):
        make_test_config(tmp_path, overrides={"data_dir": "/var/lib/aurora"})
