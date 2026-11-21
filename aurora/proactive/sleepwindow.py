"""Sleep-window learner.

Learns the user's quiet hours from the hours-of-day when messages actually
happen (adapted from Shikigami's sleep_window.py): an hour counts as quiet
when it has seen almost no activity across recorded days. The learned
window only ever *narrows* the config quiet hours, never widens them, and
needs at least a few days of data before it takes effect. The point: the
companion should not propose topics at 4 a.m. unless you keep 4 a.m. hours.
"""

from __future__ import annotations

from ..clock import Clock
from ..storage import Database

REQUIRED_DAYS = 3
QUIET_RATIO = 0.10


class SleepWindow:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def observe(self) -> None:
        """Record this hour-of-day in the histogram (called per user message)."""
        hour = int(self.clock.now() // 3600 % 24)
        hist = self.db.get_meta_json("sleep.hours", {})
        day = self.clock.now() // 86400
        days = hist.get("days", {})
        days.setdefault(str(day), set())
        hist["days"] = {str(d): list(v) if isinstance(v, set) else v for d, v in days.items()}
        hours = hist.get("hours", [0] * 24)
        hour_days = hist.setdefault("hour_days", {str(h): [] for h in range(24)})
        entry = hour_days.setdefault(str(hour), [])
        if day not in entry:
            entry.append(str(day))
            hours[hour] = hours[hour] + 1 if hour < len(hours) else 1
        hist["hours"] = hours
        self.db.set_meta_json("sleep.hours", hist)

    def learned_quiet_hours(self) -> tuple[int, int] | None:
        """(start_hour, end_hour) of the longest quiet run touching the night,
        or None when there is not enough data."""
        hist = self.db.get_meta_json("sleep.hours", {})
        hour_days = hist.get("hour_days", {})
        if len(hour_days.get("0", [])) < REQUIRED_DAYS:
            return None
        max_days = max((len(v) for v in hour_days.values()), default=0)
        if max_days == 0:
            return None
        quiet = [h for h in range(24) if len(hour_days.get(str(h), [])) / max_days <= QUIET_RATIO]
        if len(quiet) < 3:
            return None
        # Longest contiguous run (wrapping around midnight).
        best = (quiet[0], quiet[0])
        run_start = prev = quiet[0]
        for h in quiet[1:] + [quiet[0] + 24]:
            if h == prev + 1:
                prev = h
                continue
            if prev - run_start > best[1] - best[0]:
                best = (run_start, prev % 24)
            run_start = prev = h
        if prev - run_start > best[1] - best[0]:
            best = (run_start, prev % 24)
        return best

    def is_quiet(self, quiet_start: str, quiet_end: str) -> bool:
        """True inside the configured quiet window, tightened by learned data."""
        def parse(hhmm: str) -> float:
            hh, mm = hhmm.split(":")
            return int(hh) + int(mm) / 60.0

        now = self.clock.now()
        hour = (now / 3600.0) % 24
        start, end = parse(quiet_start), parse(quiet_end)
        in_config = (hour >= start or hour < end) if start > end else (start <= hour < end)
        if not in_config:
            return False
        learned = self.learned_quiet_hours()
        if learned is None:
            return in_config
        ls, le = float(learned[0]), float(learned[1])
        in_learned = (hour >= ls or hour < le) if ls > le else (ls <= hour < le)
        # learned window narrows only: stay quiet only if inside both
        return in_learned
