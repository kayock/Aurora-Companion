"""Multi-turn regressions for reminder fixation and topic following."""

from __future__ import annotations

import pytest

from aurora.conversation.focus import (
    close_loop_reply,
    is_confirm_ask,
    is_rejection,
    is_test_context,
    should_close_schedule_loop,
    topic_change_away_from_schedule,
)
from aurora.reminders.intent import UNAUTHORIZED_SCHEDULE_MSG


def test_rejection_and_test_detectors():
    assert is_rejection("No.")
    assert is_rejection("no thanks")
    assert is_test_context(
        "No, that is a test just to see if you worked and you worked fine. Thank you."
    )
    assert is_confirm_ask(
        "Can you please confirm whether you'd like me to set a reminder for "
        '"Test reminders" every Monday at 9am?'
    )
    assert "do not ask them to confirm" in UNAUTHORIZED_SCHEDULE_MSG.lower()
    assert "ask them to confirm before" not in UNAUTHORIZED_SCHEDULE_MSG.lower()


def test_should_close_on_no_and_test_only():
    assert should_close_schedule_loop(
        "No.",
        assistant_asking_confirm=True,
        recent_reminder_delivery=False,
        pending_confirm=False,
    )
    assert should_close_schedule_loop(
        "that was just a test",
        assistant_asking_confirm=False,
        recent_reminder_delivery=True,
        pending_confirm=False,
    )
    # Substantive topic change is NOT a canned close — model still answers.
    assert not should_close_schedule_loop(
        "I am fine trying to get you working so we can get your personality evolving",
        assistant_asking_confirm=False,
        recent_reminder_delivery=True,
        pending_confirm=False,
    )
    assert topic_change_away_from_schedule(
        "I am fine trying to get you working so we can get your personality evolving",
        recent_reminder_delivery=True,
    )


@pytest.mark.asyncio
async def test_screenshot_flow_rejection_closes_without_reconfirm(make_app):
    """Suggestion → rejection → no repeated confirmation."""
    sticky = (
        "You're right, I should have asked for confirmation before scheduling "
        "the reminder. Let me ask again: would you like to schedule a reminder "
        'for "Test reminders" every Monday at 9am?'
    )
    app, provider = make_app(default=sticky)
    rem = app.reminders.add("Test reminders", app.clock.now() - 1, source="user")
    await app.conversation.deliver_reminder(rem)

    # Owner explains it was a test — deterministic close, not another confirm.
    provider.default = sticky
    msg = await app.conversation.submit_user_message(
        "No, that is a test just to see if you worked and you worked fine. Thank you."
    )
    assert "confirm" not in msg["content"].lower()
    assert "monday" not in msg["content"].lower()
    assert "test" in msg["content"].lower() or "glad" in msg["content"].lower()

    # Repeated bare No after a confirm-ask must also close, not loop.
    app.db.execute(
        "INSERT INTO messages (role, content, ts, person, source) VALUES (?,?,?,?,?)",
        ("assistant", sticky, app.clock.now(), "owner", "chat"),
    )
    app.db.set_meta("reminder.pending_confirm", "1")
    msg2 = await app.conversation.submit_user_message("No.")
    assert "confirm whether" not in msg2["content"].lower()
    assert "would you like to schedule" not in msg2["content"].lower()
    body = close_loop_reply("No.")
    assert "won't set" in body.lower() or "won't" in body.lower()


@pytest.mark.asyncio
async def test_topic_change_after_delivery_does_not_reopen_reminder(make_app):
    """Owner talks about AI projects after a delivery — no confirm loop."""
    confirm = (
        "You're right, I should have waited for your confirmation before "
        'setting the reminder. Can you please confirm whether you\'d like me '
        'to set a reminder for "Test reminders" every Monday at 9am?'
    )
    app, provider = make_app(default=confirm)
    rem = app.reminders.add("Test reminders", app.clock.now() - 1, source="user")
    await app.conversation.deliver_reminder(rem)

    # Model would stick; focus + loop clear should prevent confirm-ask context.
    provider.default = (
        "That sounds like a great day off plan — let's get personality evolving "
        "alongside your AI RPG platform and other projects. Where should we start?"
    )
    msg = await app.conversation.submit_user_message(
        "I am fine trying to get you working so we can get your personality "
        "evolving and on my Saturday morning AI RPG platform and of course "
        "all my other AI projects since today's my day off."
    )
    low = msg["content"].lower()
    assert "confirm" not in low
    assert "monday" not in low
    assert "test reminders" not in low
    # Window must not keep feeding confirm-ask rows when topic moved on.
    window = app.conversation._window(current_text="Let's talk about my AI RPG platform.")
    assert not any(is_confirm_ask(m["content"]) for m in window)


@pytest.mark.asyncio
async def test_memory_reference_continues_without_reminder_fixation(make_app):
    app, provider = make_app(
        default="I remember you enjoy thoughtful conversation. How is the RPG coming along?"
    )
    app.store.add_fact(
        "Eric works on a Saturday morning AI RPG platform",
        subject="owner", source="user_command", category="project",
    )
    rem = app.reminders.add("stretch", app.clock.now() - 1, source="user")
    await app.conversation.deliver_reminder(rem)
    msg = await app.conversation.submit_user_message(
        "Do you remember what I said about my AI projects?"
    )
    low = msg["content"].lower()
    assert "confirm" not in low
    assert "schedule" not in low or "rpg" in low or "project" in low or "remember" in low
