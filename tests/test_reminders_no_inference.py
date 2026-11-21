"""Reminders and daily operation with NO inference at all: scheduling,
firing, missed replay, honest replies — the companion stays useful when
no model is reachable or configured."""

import asyncio

import pytest

from aurora.app import CompanionApp
from aurora.clock import Clock
from tests.conftest import SEED, T0, make_test_config


def _bare_app(tmp_path, overrides=None):
    """A CompanionApp without any chat/background provider (no preset that
    could be auto-created from config)."""
    merged = {"provider.chat": "__no_such_preset__"}
    if overrides:
        merged.update(overrides)
    cfg = make_test_config(tmp_path, merged)
    return CompanionApp(cfg, clock=Clock(T0), seed_path=SEED,
                        chat_provider=None, background_provider=None)


async def test_reminder_command_works_without_model(tmp_path):
    app = _bare_app(tmp_path)
    msg = await app.conversation.submit_user_message("/remind in 10 minutes to flip the laundry")
    assert msg["role"] == "assistant"
    assert "Scheduled reminder" in msg["content"]
    active = app.reminders.list(active_only=True)
    assert len(active) == 1
    assert abs(active[0].due_at - (T0 + 600)) < 5


async def test_reminder_fires_without_model_plain_text(tmp_path):
    app = _bare_app(tmp_path)
    rem = app.reminders.add("Water the ferns", T0 - 1)  # already due
    await app.reminder_scheduler.fire_due()
    hist = app.conversation.history()
    delivered = [m for m in hist if m["source"] == "reminder" and m["role"] == "assistant"]
    assert any("Water the ferns" in m["content"] and "⏰" in m["content"] for m in delivered)
    assert app.reminders.get(rem.id).status in ("delivered", "done")


async def test_plain_chat_gets_honest_no_model_reply(tmp_path):
    app = _bare_app(tmp_path)
    msg = await app.conversation.submit_user_message("hello?")
    assert "No chat model is configured" in msg["content"]
    assert msg["meta"] == {}  # not a model error — the honest no-provider path


async def test_recurring_reminder_keeps_schedule_without_model(tmp_path):
    app = _bare_app(tmp_path)
    app.reminders.add("Stand up", T0 + 60, repeat={"kind": "interval", "seconds": 1800})
    app.clock.advance(61)
    await app.reminder_scheduler.fire_due()
    rem = app.reminders.list(active_only=True)[0]
    assert rem.status == "scheduled" and rem.due_at > T0  # rescheduled
    app.clock.advance(1800)
    await app.reminder_scheduler.fire_due()
    assert len([d for d in app.reminder_scheduler.deliveries]) == 2


async def test_missed_reminders_replayed_on_restart_without_model(tmp_path):
    app = _bare_app(tmp_path)
    rem = app.reminders.add("Feed the cat", T0 + 30)
    app.db.close()

    # restart far in the future: the reminder came due while "Aurora was off"
    app2 = _bare_app(tmp_path)
    try:
        app2.clock.advance(7200)
        app2.reminder_scheduler.start()  # startup recovery hook
        await asyncio.sleep(0.1)
        missed = app2.reminders.get(rem.id)
        assert missed is not None and missed.status in ("delivered", "done")
        hist = app2.conversation.history()
        assert any("came due while Aurora was not running" in m["content"]
                   for m in hist if m["role"] == "assistant")
    finally:
        app2.reminder_scheduler._running = False


async def test_memory_and_commands_still_work_without_model(tmp_path):
    app = _bare_app(tmp_path)
    msg = await app.conversation.submit_user_message("Remember that the greenhouse timer is at 6am")
    hist = app.conversation.history()
    assert any("Noted for keeps" in m["content"] for m in hist)
    assert any("greenhouse timer" in f.content for f in app.store.active_facts())
    reply = await app.conversation.submit_user_message("/facts")
    assert "greenhouse" in reply["content"]


async def test_proactive_fallback_text_without_model(tmp_path):
    app = _bare_app(tmp_path)
    msg = await app.conversation.send_proactive("check_in", None, 0.3)
    assert msg["role"] == "assistant" and msg["source"] == "proactive"
    assert msg["content"]  # honest fallback content, still journaled
    rows = app.db.query("SELECT kind FROM proactive_log")
    assert any(r["kind"] == "check_in" for r in rows)


async def test_no_model_post_turn_hooks_do_not_crash(tmp_path):
    app = _bare_app(tmp_path)
    msg = await app.conversation.submit_user_message("just a plain line")
    await asyncio.sleep(0.15)  # let fire-and-forget hooks run
    assert msg["role"] == "assistant"
    # lexical-only consolidation still works without any provider: the store
    # blocks near-duplicates at add time, so the second copy is written with
    # allow_duplicate (e.g. imported from an old export) for the merge phase
    app.store.add_fact("Backup day is Sunday", source="manual")
    app.store.add_fact("Backup day is sunday morning", source="manual",
                       allow_duplicate=True)
    result = await app.consolidator.tick(force=True)
    assert result is not None and result["merged"] >= 1
