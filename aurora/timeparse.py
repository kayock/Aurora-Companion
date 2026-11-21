"""Natural-language time parsing and repeat rules.

Design principle taken from Luna Local Agent (AGPL-3.0): never let the
LLM do date arithmetic. The model passes the user's own timing words to
`parse_when` / `parse_repeat`; this module turns them into exact
timestamps and recurrence rules. Reimplemented compactly for Aurora with
an injectable clock so tests can freeze time.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

MIN_REPEAT_SECONDS = 60

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
for _abbr, _num in [("mon", 0), ("tue", 1), ("tues", 1), ("wed", 2), ("thu", 3),
                    ("thur", 3), ("thurs", 3), ("fri", 4), ("sat", 5), ("sun", 6)]:
    WEEKDAYS.setdefault(_abbr, _num)

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}
for _abbr, _num in [("jan", 1), ("feb", 2), ("mar", 3), ("apr", 4), ("jun", 6),
                    ("jul", 7), ("aug", 8), ("sep", 9), ("sept", 9), ("oct", 10),
                    ("nov", 11), ("dec", 12)]:
    MONTHS.setdefault(_abbr, _num)

# A clock time requires a colon ("18:30") or am/pm ("7pm") — bare numbers
# like the day in "nov 27" are never mistaken for hours.
TIME_COLON_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*(a\.?m\.?|p\.?m\.?)?", re.I)
TIME_AMPM_RE = re.compile(r"\b(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)", re.I)

IN_RE = re.compile(
    r"\bin\s+((?:\d+|a\s+few|several|some|an?\b)\s*(?:\d+\s*)?"
    r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?)"
    r"(?:\s*(?:and\s+)?(?:\d+|an?\b)?\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?))?)",
    re.I)

UNIT_SECONDS = {
    "second": 1, "sec": 1, "secs": 1, "seconds": 1,
    "minute": 60, "min": 60, "mins": 60, "minutes": 60,
    "hour": 3600, "hr": 3600, "hrs": 3600, "hours": 3600,
    "day": 86400, "days": 86400,
    "week": 604800, "weeks": 604800,
    "month": 2592000, "months": 2592000,
}

_IN_WORD_AMOUNTS = {"a": 1, "an": 1, "a few": 3, "several": 3, "some": 2}


def _base_dt(now_epoch: float, tz_offset_minutes: int = 0) -> datetime:
    tz = timezone(timedelta(minutes=tz_offset_minutes)) if tz_offset_minutes else timezone.utc
    return datetime.fromtimestamp(now_epoch, tz)


def _find_clock(text: str) -> tuple[int, int] | None:
    m = TIME_COLON_RE.search(text)
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "")
    else:
        m = TIME_AMPM_RE.search(text)
        if not m:
            return None
        hour, minute, ampm = int(m.group(1)), 0, m.group(2)
    ampm = ampm.lower().replace(".", "")
    if ampm.startswith("p"):
        if hour < 12:
            hour += 12
    elif ampm.startswith("a") and hour == 12:
        hour = 0
    if hour > 23:
        return None
    return hour, minute


def parse_when(text: str, now_epoch: float, default_hour: int = 9,
               tz_offset_minutes: int | None = None) -> float | None:
    """Parse a time expression into an absolute epoch.

    Accepts: "in 5 minutes", "in 2 hours 30 mins", "in a few hours",
    "at 18:30", "at 7pm", "tomorrow", "tomorrow at 9am", "tonight",
    "this evening", "next monday", "on friday at 6", "nov 25 at 10am",
    "25 nov" via "nov 25", "2026-11-25", "2026-11-25 14:00", bare "8am".
    Returns None if nothing time-like is found.
    """
    if tz_offset_minutes is None:
        tz_offset_minutes = 0
    base = _base_dt(now_epoch, tz_offset_minutes)
    text = text.strip().lower()
    if not text:
        return None

    # ISO datetime / date first.
    iso = re.search(r"(\d{4})-(\d{2})-(\d{2})(?:[ t](\d{1,2}):(\d{2}))?", text)
    clock = _find_clock(text)
    if iso:
        try:
            hour = int(iso.group(4)) if iso.group(4) else default_hour
            minute = int(iso.group(5) or 0)
            if clock and iso.group(4) is None:
                hour, minute = clock
            dt = base.replace(year=int(iso.group(1)), month=int(iso.group(2)),
                              day=int(iso.group(3)), hour=hour, minute=minute,
                              second=0, microsecond=0)
        except ValueError:
            return None
        return dt.timestamp()

    # "in N minutes/hours/..." — relative offset. Bare durations ("15 minutes",
    # "2 hours 30 mins", "for 5 minutes") parse the same way, so both phrasings
    # work everywhere (chat, dashboard form, API).
    inm = IN_RE.search(text)
    segment = None
    if inm and "at" not in text:
        segment = inm.group(1)
    else:
        bare = re.sub(r"^\s*for\s+", "", text).strip()
        if bare:
            residue = re.sub(
                r"\b(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?"
                r"|a|an|the|few|and|half)\b|\d+", "", bare)
            if not residue.strip(" .,"):  # only duration words + digits remain
                segment = bare
    if segment:
        for word, amount in _IN_WORD_AMOUNTS.items():
            segment = re.sub(rf"\b{word}\b", str(amount), segment)
        delta = timedelta(0)
        for amount, unit in re.findall(
                r"(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?)", segment):
            try:
                n = int(amount)
            except ValueError:
                continue
            delta += timedelta(seconds=n * UNIT_SECONDS[unit])
        if delta > timedelta(0):
            return (base + delta).timestamp()

    day = None
    tomorrow = re.search(r"\btomorrow\b", text)
    tonight = re.search(r"\btonight\b", text)
    this_part = re.search(r"\bthis\s+(evening|afternoon|morning|night)\b", text)
    weekday = None
    for wd, num in WEEKDAYS.items():
        if re.search(rf"\b(?:next\s+|on\s+|this\s+)?{wd}\b", text):
            weekday = num
            break
    month_first = None
    for mo, num in MONTHS.items():
        m = re.search(rf"\b{mo}\w*\s+(\d{{1,2}})\b", text)
        if m:
            month_first = (num, int(m.group(1)))
            break

    hour = minute = None
    if clock:
        hour, minute = clock
    if hour is None:
        bare_hour = re.search(r"\bat\s+(\d{1,2})\b(?!\d)", text)
        if bare_hour:
            hour = int(bare_hour.group(1))

    if month_first:
        try:
            day = base.replace(month=month_first[0], day=month_first[1])
            if day <= base:
                day = day.replace(year=day.year + 1)
        except ValueError:
            return None
    elif weekday is not None:
        day = base + timedelta(days=(weekday - base.weekday()) % 7 or 7)
        if re.search(r"\bnext\b", text) and (weekday - base.weekday()) % 7 == 0:
            day = day + timedelta(days=7)
    elif tomorrow:
        day = base + timedelta(days=1)
    elif tonight or this_part:
        day = base

    if day is None:
        if hour is None:
            return None
        day = base

    if hour is None:
        if tonight:
            hour, minute = 21, 0
        elif this_part:
            label = this_part.group(1)
            hour, minute = {"morning": (8, 0), "afternoon": (14, 0),
                            "evening": (19, 0), "night": (21, 0)}[label]
        else:
            hour, minute = default_hour, 0

    try:
        dt = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
    # A bare clock earlier today on an unqualified day rolls to tomorrow.
    if not tomorrow and weekday is None and month_first is None and not tonight and not this_part:
        if hour < base.hour:
            dt = dt + timedelta(days=1)
    return dt.timestamp()


def parse_repeat(text: str) -> dict | None:
    """Parse a recurrence rule: "every day at 8am" -> daily; "every morning"
    -> daily 08:00; "every 30 minutes" -> interval; "weekly on friday at 9pm"
    -> weekly; "every monday" -> weekly."""
    text = text.lower()

    interval = re.search(
        r"every\s+(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)", text)
    if interval:
        n = int(interval.group(1))
        seconds = n * UNIT_SECONDS[interval.group(2)]
        return {"kind": "interval", "seconds": max(seconds, MIN_REPEAT_SECONDS)}

    clock = _find_clock(text)
    hour, minute = clock if clock else (None, None)

    daily = re.search(r"\b(every\s+day|daily|every\s+(?:morning|evening|night|afternoon)|everyday)\b", text)
    if daily:
        label = re.search(r"(morning|afternoon|evening|night)", daily.group(0))
        if hour is None and label:
            hour, minute = {"morning": (8, 0), "afternoon": (14, 0),
                            "evening": (19, 0), "night": (21, 0)}[label.group(1)]
        return {
            "kind": "daily",
            "hour": 8 if hour is None else hour,
            "minute": minute or 0,
            "weekdays_only": bool(re.search(r"\bweekdays?\b", text)),
        }

    weekly = bool(re.search(r"\b(every\s+week|weekly)\b", text)) or (
        re.search(r"\bevery\s+\w+\b", text) and _weekday_in(text) is not None)
    if weekly:
        wd = _weekday_in(text) or 0
        return {"kind": "weekly", "weekday": wd, "hour": hour if hour is not None else 9,
                "minute": minute or 0}
    return None


def _weekday_in(text: str) -> int | None:
    for wd, num in WEEKDAYS.items():
        if re.search(rf"\b{wd}\b", text):
            return num
    return None


def next_occurrence(repeat: dict, after_epoch: float) -> float | None:
    """Next fire time for a repeat rule strictly after `after_epoch`."""
    base = datetime.fromtimestamp(after_epoch)
    kind = repeat.get("kind")
    if kind == "interval":
        seconds = max(int(repeat.get("seconds", 60)), MIN_REPEAT_SECONDS)
        return after_epoch + seconds
    if kind == "daily":
        try:
            candidate = base.replace(hour=int(repeat.get("hour", 8)),
                                     minute=int(repeat.get("minute", 0)),
                                     second=0, microsecond=0)
        except ValueError:
            return None
        if candidate <= base:
            candidate += timedelta(days=1)
        if repeat.get("weekdays_only"):
            while candidate.weekday() >= 5:
                candidate += timedelta(days=1)
        return candidate.timestamp()
    if kind == "weekly":
        wd = int(repeat.get("weekday", 0))
        try:
            candidate = base.replace(hour=int(repeat.get("hour", 9)),
                                     minute=int(repeat.get("minute", 0)),
                                     second=0, microsecond=0)
        except ValueError:
            return None
        candidate += timedelta(days=(wd - candidate.weekday()) % 7)
        if candidate <= base:
            candidate += timedelta(days=7)
        return candidate.timestamp()
    return None


def describe_repeat(repeat: dict) -> str:
    kind = repeat.get("kind")
    if kind == "interval":
        seconds = int(repeat.get("seconds", 60))
        if seconds % 3600 == 0 and seconds >= 3600:
            return f"every {seconds // 3600} hour(s)"
        if seconds % 60 == 0:
            return f"every {seconds // 60} minute(s)"
        return f"every {seconds} seconds"
    if kind == "daily":
        wd = ", weekdays only" if repeat.get("weekdays_only") else ""
        return f"every day at {int(repeat.get('hour', 8)):02d}:{int(repeat.get('minute', 0)):02d}{wd}"
    if kind == "weekly":
        names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        wd = int(repeat.get("weekday", 0))
        return f"every {names[wd]} at {int(repeat.get('hour', 9)):02d}:{int(repeat.get('minute', 0)):02d}"
    return ""


def friendly(epoch: float, now_epoch: float) -> str:
    """Human-friendly relative time ("in 3 hours", "2 hours ago")."""
    delta = epoch - now_epoch
    future = delta >= 0
    seconds = abs(delta)
    if seconds < 60:
        span = "moments"
    elif seconds < 3600:
        span = f"{int(seconds // 60)} minute(s)"
    elif seconds < 86400:
        span = (f"{seconds / 3600:.1f} hour(s)" if seconds < 7200
                else f"{int(seconds // 3600)} hour(s)")
    else:
        span = f"{int(seconds // 86400)} day(s)"
    return f"in {span}" if future else f"{span} ago"


def too_frequent(repeat: dict, min_seconds: int = MIN_REPEAT_SECONDS) -> bool:
    if repeat and repeat.get("kind") == "interval":
        return int(repeat.get("seconds", 0)) < min_seconds
    return False
