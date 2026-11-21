"""Reminder store + scheduler semantics: recurrence, follow-up, snooze,
missed recovery, model-independent delivery."""

import pytest

from aurora.reminders.scheduler import ReminderScheduler
from aurora.reminders.store import ReminderStore


@pytest.fixture
def sched(make_app):
    app, _ = make_app(overrides={"proactive.enabled": False})
    delivered = []

    async def on_due(rem, note):
        delivered.append((rem.id, note))

    rs = ReminderScheduler(app.reminders, app.clock, on_due)
    return app, app.reminders, rs, delivered


async def test_one_time_delivery(sched):
    app, store, rs, delivered = sched
    rem = store.add("Call the dentist", app.clock.now() + 60)
    app.clock.advance(61)
    fired = await rs.fire_due()
    assert any(r.id == rem.id for r in fired)
    assert delivered and delivered[0][0] == rem.id
    assert store.get(rem.id).status == "delivered"


async def test_not_due_yet(sched):
    app, store, rs, delivered = sched
    store.add("Later thing", app.clock.now() + 3600)
    fired = await rs.fire_due()
    assert fired == []


async def test_recurring_reschedules(sched):
    app, store, rs, delivered = sched
    rem = store.add("Water the plants", app.clock.now() + 60,
                    repeat={"kind": "interval", "seconds": 1800})
    app.clock.advance(61)
    await rs.fire_due()
    after = store.get(rem.id)
    assert after.status == "scheduled"
    assert after.due_at > app.clock.now()
    app.clock.advance(1800)
    await rs.fire_due()
    assert len(delivered) == 2


async def test_follow_up_until_acknowledged(sched):
    app, store, rs, delivered = sched
    rem = store.add("Take the laundry out", app.clock.now() + 60, follow_up=True,
                    follow_up_seconds=300, follow_up_max=3)
    # first fire
    app.clock.advance(61)
    await rs.fire_due()
    assert store.get(rem.id).status == "waiting_ack"
    # follow-ups fire every 300s until max attempts
    app.clock.advance(300)
    await rs.fire_due()
    app.clock.advance(300)
    await rs.fire_due()
    assert len(delivered) == 3
    # attempts exhausted -> done (auto-finished, NOT acked)
    app.clock.advance(300)
    await rs.fire_due()
    final = store.get(rem.id)
    assert final.status == "done" and final.attempts == 3


async def test_ack_stops_follow_up(sched):
    app, store, rs, delivered = sched
    rem = store.add("Check the oven", app.clock.now() + 60, follow_up=True,
                    follow_up_seconds=300, follow_up_max=5)
    app.clock.advance(61)
    await rs.fire_due()
    store.acknowledge(rem.id)
    app.clock.advance(3000)
    await rs.fire_due()
    assert len(delivered) == 1
    final = store.get(rem.id)
    assert final.status == "done" and final.ack


async def test_recurring_ack_schedules_next_occurrence(sched):
    app, store, rs, delivered = sched
    rem = store.add("Stretch break", app.clock.now() + 60,
                    repeat={"kind": "interval", "seconds": 900}, follow_up=True,
                    follow_up_seconds=120, follow_up_max=1)
    app.clock.advance(61)
    await rs.fire_due()
    store.acknowledge(rem.id)
    after = store.get(rem.id)
    assert after.status == "scheduled" and after.due_at > app.clock.now()


async def test_snooze(sched):
    app, store, rs, delivered = sched
    rem = store.add("Ping me later", app.clock.now() + 10, follow_up=True,
                    follow_up_seconds=300, follow_up_max=5)
    app.clock.advance(11)
    await rs.fire_due()  # waiting_ack
    store.snooze(rem.id, 600)
    assert store.get(rem.id).snooze_count == 1


async def test_snoozed_reminder_refires(sched):
    app, store, rs, delivered = sched
    rem = store.add("Snooze test", app.clock.now() + 10, follow_up=True,
                    follow_up_seconds=60, follow_up_max=4)
    app.clock.advance(11)
    await rs.fire_due()  # now waiting_ack
    store.snooze(rem.id, 120)
    got = store.get(rem.id)
    assert got.status == "snoozed"
    app.clock.advance(121)
    fired = await rs.fire_due()
    assert any(r.id == rem.id for r in fired)


async def test_missed_recovery_on_restart(sched):
    app, store, rs, delivered = sched
    rem = store.add("Missed while closed", app.clock.now() + 60)
    # simulate app closed: clock jumps far ahead, new scheduler, same store
    app.clock.advance(7200)
    rs2 = ReminderScheduler(store, app.clock, rs.on_due)
    handled = await rs2.recover_missed()
    assert any(h["id"] == rem.id for h in handled)
    assert handled[0]["replayed"] is True
    assert store.get(rem.id).status == "delivered"  # replayed once
    assert "not running" in delivered[0][1]


async def test_missed_recurring_reschedules_forward(sched):
    app, store, rs, delivered = sched
    rem = store.add("Daily standup", app.clock.now() + 60,
                    repeat={"kind": "interval", "seconds": 3600})
    app.clock.advance(10000)
    rs2 = ReminderScheduler(store, app.clock, rs.on_due)
    await rs2.recover_missed()
    after = store.get(rem.id)
    assert after.status == "scheduled"
    assert after.due_at > app.clock.now()


async def test_delivery_without_model(tmp_path):
    """No provider at all: reminders still fire as plain messages."""
    from aurora.app import CompanionApp
    from aurora.clock import Clock
    from tests.conftest import make_test_config as test_config, SEED, T0

    cfg = test_config(tmp_path)
    cfg.set("provider.presets", {})  # genuinely no provider available
    cfg.set("provider.chat", "none-configured")
    app = CompanionApp(cfg, clock=Clock(T0), seed_path=SEED)
    try:
        await app.reminder_scheduler.stop()
        rem = app.reminders.add("Model-less reminder", app.clock.now() + 60)
        app.clock.advance(61)
        await app.reminder_scheduler.fire_due()
        rows = app.db.query("SELECT content FROM messages WHERE source='reminder'")
        assert any("Model-less reminder" in r["content"] for r in rows)
    finally:
        app.db.close()


async def test_store_persistence_basics(make_app):
    app, _ = make_app()
    store = app.reminders
    rem = store.add("Persist me", app.clock.now() + 3600, repeat={"kind": "daily", "hour": 8, "minute": 30},
                    project="home", follow_up=True)
    fetched = store.get(rem.id)
    assert fetched.repeat["kind"] == "daily"
    assert fetched.project == "home"
    assert fetched.follow_up["every_seconds"] == 600
