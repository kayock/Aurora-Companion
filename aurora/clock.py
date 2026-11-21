"""Injectable clock.

Every time-sensitive service (reminders, proactive scheduler, mood decay,
consolidation triggers) reads time through a Clock so tests can advance
virtual time deterministically and prove persistence/scheduling behaviour
without sleeping.
"""

from __future__ import annotations

import time as _time


class Clock:
    def __init__(self, start: float | None = None) -> None:
        self._virtual: float | None = start

    def now(self) -> float:
        if self._virtual is not None:
            return self._virtual
        return _time.time()

    def advance(self, seconds: float) -> None:
        """Only meaningful for a virtual clock; a real clock ignores it."""
        if self._virtual is not None:
            self._virtual += seconds


class RealClock(Clock):
    def __init__(self) -> None:
        super().__init__(None)
