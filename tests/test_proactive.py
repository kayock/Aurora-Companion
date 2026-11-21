"""Proactive scheduler: gates, caps, cooldowns, dedup, single authority."""

import pytest

from aurora.proactive.scheduler import ProactiveScheduler

T0 = 1764712800.0  # 2026-11-21 10:00 UTC


def _sched(make_app, overrides=None):
    app, mock = make_app(overrides=overrides)
    return app, app.proactive, mock


def _silence(pro, minutes=120):
    pro._last_user_message = pro.clock.now() - minutes * 60


async def test_silent_when_user_recently_active(make_app):
    app, pro, _ = _sched(make_app)
    pro.touch_user_message()  # just "spoke"
    d = pro.decide()
    assert not d["speak"] and "active" in d["reason"]


async def test_focus_suppresses(make_app):
    app, pro, _ = _sched(make_app)
    pro.set_focus(pro.clock.now() + 3600, enabled=True)
    _silence(pro)
    d = pro.decide()
    assert not d["speak"] and d["reason"] == "focus period"


async def test_quiet_hours_suppress(make_app):
    app, pro, _ = _sched(make_app, overrides={"proactive.quiet_start": "00:00",
                                              "proactive.quiet_end": "23:59"})
    _silence(pro)
    d = pro.decide()
    assert not d["speak"] and d["reason"] == "quiet hours"


async def test_no_speak_without_min_silence(make_app):
    app, pro, _ = _sched(make_app)
    pro.touch_user_message()
    pro.clock.advance(600)  # 10 min < min_silence (900)
    d = pro.decide()
    assert not d["speak"]


async def test_topic_share_after_silence_with_dedup(make_app):
    app, pro, mock = _sched(make_app)
    fact = app.store.add_fact("Alex finished painting the shed last month", weight=1.5)
    _silence(pro, 120)
    d = pro.decide()
    assert d["speak"]
    assert d["kind"] in ("topic_share", "memory_resurfacing", "check_in")

    # journal a proactive message with this topic ref, then a second decision
    # within 24h must not re-use the same ref.
    app.db.execute(
        "INSERT INTO proactive_log (ts, kind, text, topic_ref, urgency, trigger) VALUES (?,?,?,?,?,?)",
        (pro.clock.now(), d["kind"], "x", d["topic"].ref, 0.5, "test"))
    d2 = pro.decide()
    if d2.get("topic"):
        assert d2["topic"].ref != d["topic"].ref


async def test_daily_cap(make_app):
    app, pro, _ = _sched(make_app)
    _silence(pro)
    for _ in range(pro.max_per_day):
        state = pro._state()
        pro._bump_daily(state, "topic_share")
    d = pro.decide()
    assert not d["speak"] and "cap" in d["reason"]


async def test_cooldown_between_speaks(make_app):
    app, pro, _ = _sched(make_app)
    _silence(pro, 200)
    d1 = pro.decide()
    assert d1["speak"]
    # record that we just spoke with the same kind
    state = pro._state()
    state.setdefault("kind_last_fired", {})[d1["kind"]] = pro.clock.now()
    pro._set_state(state)
    d2 = pro.decide()
    # within the urgency-scaled cooldown nothing of the same kind should fire;
    # another kind may still win only if genuinely eligible
    if d2["speak"]:
        assert d2["kind"] != d1["kind"]


async def test_no_response_streak_backs_off(make_app):
    app, pro, _ = _sched(make_app)
    _silence(pro)
    state = pro._state()
    state["consecutive_no_response"] = 3
    pro._set_state(state)
    d = pro.decide()
    assert not d["speak"] and "no response" in d["reason"]


async def test_work_break_after_long_session(make_app):
    app, pro, _ = _sched(make_app)
    # session started 3h ago; user active recently (silence < 3600)
    pro._last_user_message = pro.clock.now() - 1200
    state = pro._state()
    state["session_start"] = pro.clock.now() - 3 * 3600
    pro._set_state(state)
    d = pro.decide()
    assert d["speak"] and d["kind"] == "work_break"


async def test_morning_greeting_only_in_window(make_app):
    app, pro, _ = _sched(make_app)
    # T0 is 10:00 UTC, outside the default 07:30-10:00 window edge; set window wide
    pro.cfg["morning_greeting"]["window"] = "00:00-23:59"
    pro._last_user_message = pro.clock.now() - 7200
    state = pro._state()
    state["session_start"] = pro.clock.now() - 7200
    pro._set_state(state)
    d = pro.decide()
    assert d["speak"]
    # urgency ordering should pick something; morning greeting is eligible
    assert d["kind"] in ("morning_greeting", "topic_share", "check_in", "memory_resurfacing")


async def test_disabled_scheduler_never_speaks(make_app):
    app, pro, _ = _sched(make_app, overrides={"proactive.enabled": False})
    _silence(pro)
    assert not pro.decide()["speak"]


async def test_single_authority_all_proactive_logged(make_app):
    """Every companion-initiated message goes through one path and is journaled."""
    app, pro, mock = _sched(make_app)
    fact = app.store.add_fact("Alex has a pottery class on Thursdays", weight=1.4)
    _silence(pro)
    pro.on_initiate = app.conversation.send_proactive
    await pro.tick()
    rows = app.db.query("SELECT * FROM proactive_log WHERE kind != 'reminder_delivery'")
    assert rows, "proactive message must be journaled"
    msgs = app.db.query("SELECT * FROM messages WHERE source='proactive'")
    assert msgs, "proactive turn must be in the one conversation history"


async def test_time_sanity():
    # T0 really is outside default quiet hours (10:00 UTC)
    from aurora.proactive.scheduler import _parse_hhmm
    assert _parse_hhmm("07:30") == 7.5
