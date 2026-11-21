"""Time parser and recurrence rules (frozen clock)."""

import time

from aurora.timeparse import (describe_repeat, friendly, next_occurrence,
                              parse_repeat, parse_when, too_frequent)

NOW = 1795255200.0  # 2026-11-21 10:00 UTC (Saturday)


def test_in_minutes():
    due = parse_when("in 5 minutes", NOW)
    assert due == NOW + 300


def test_in_mixed_units():
    due = parse_when("in 1 hour and 30 mins", NOW)
    assert due == NOW + 5400


def test_at_clock():
    due = parse_when("at 18:30", NOW)
    expected = time.strftime("%Y-%m-%d", time.gmtime(NOW)) + "T18:30"
    import datetime as dt
    d = dt.datetime.fromtimestamp(due, dt.timezone.utc)
    assert (d.hour, d.minute) == (18, 30)


def test_at_7pm():
    import datetime as dt
    due = parse_when("at 7pm", NOW)
    d = dt.datetime.fromtimestamp(due, dt.timezone.utc)
    assert (d.hour, d.minute) == (19, 0)


def test_tomorrow_at_9():
    import datetime as dt
    due = parse_when("tomorrow at 9am", NOW)
    d = dt.datetime.fromtimestamp(due, dt.timezone.utc)
    assert d.day == 22 and d.hour == 9


def test_next_monday():
    import datetime as dt
    due = parse_when("next monday at 3pm", NOW)  # 2026-11-21 is a Saturday
    d = dt.datetime.fromtimestamp(due, dt.timezone.utc)
    assert d.weekday() == 0 and d.hour == 15
    assert d.day == 23


def test_iso_datetime():
    due = parse_when("2026-11-25 14:00", NOW)
    assert due == 1795615200.0  # 2026-11-25 14:00 UTC


def test_bare_date():
    import datetime as dt
    due = parse_when("nov 27", NOW)
    d = dt.datetime.fromtimestamp(due, dt.timezone.utc)
    assert (d.month, d.day) == (11, 27)


def test_garbage_returns_none():
    assert parse_when("whenever feels right", NOW) is None


# ---- repeat rules ----

def test_repeat_interval():
    assert parse_repeat("every 30 minutes") == {"kind": "interval", "seconds": 1800}


def test_repeat_daily():
    r = parse_repeat("every day at 8am")
    assert r["kind"] == "daily" and r["hour"] == 8


def test_repeat_every_morning():
    r = parse_repeat("every morning")
    assert r["kind"] == "daily" and r["hour"] == 8


def test_repeat_weekly():
    r = parse_repeat("weekly on friday at 9pm")
    assert r["kind"] == "weekly" and r["weekday"] == 4 and r["hour"] == 21


def test_repeat_every_monday():
    r = parse_repeat("every monday")
    assert r["kind"] == "weekly" and r["weekday"] == 0


def test_next_occurrence_daily():
    r = {"kind": "daily", "hour": 8, "minute": 0}
    nxt = next_occurrence(r, NOW)  # 10:00 -> next day 08:00
    import datetime as dt
    d = dt.datetime.fromtimestamp(nxt, dt.timezone.utc)
    assert d.day == 22 and d.hour == 8


def test_next_occurrence_weekdays_only_skips_weekend():
    r = {"kind": "daily", "hour": 9, "minute": 0, "weekdays_only": True}
    # NOW is Saturday 10:00 -> next is Monday 09:00
    nxt = next_occurrence(r, NOW)
    import datetime as dt
    d = dt.datetime.fromtimestamp(nxt, dt.timezone.utc)
    assert d.weekday() == 0 and d.day == 23


def test_describe_and_guard():
    assert "minute" in describe_repeat({"kind": "interval", "seconds": 1800})
    assert too_frequent({"kind": "interval", "seconds": 30}, min_seconds=60)
    assert not too_frequent({"kind": "interval", "seconds": 300}, min_seconds=60)


def test_friendly():
    assert "minute" in friendly(NOW + 300, NOW)
    assert "ago" in friendly(NOW - 7200, NOW)
