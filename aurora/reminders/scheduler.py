"""Reminder scheduler — dependable, model-independent.

A single asyncio task sleeps until the next due reminder (or a bounded
poll), then fires deliveries through a callback. The language model is
*never* required for a reminder to fire: the callback decides how to
present it (phrased by the model when available, plain text otherwise).
Missed reminders (app was down at due time) are detected at startup and
replayed or rescheduled according to config.

Design adapted from Luna's sleep-until-due scanner with a wake event
(adding a reminder immediately interrupts the sleep) and Shikigami's
durable store with startup missed-handling.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from ..clock import Clock
from .store import Reminder, ReminderStore

DeliveryCallback = Callable[[Reminder, str], Awaitable[None]]  # (reminder, note)

MAX_POLL_SECONDS = 30.0


class ReminderScheduler:
    def __init__(self, store: ReminderStore, clock: Clock, on_due: DeliveryCallback,
                 missed_replay: bool = True) -> None:
        self.store = store
        self.clock = clock
        self.on_due = on_due
        self.missed_replay = missed_replay
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._running = False
        self.deliveries: list[dict] = []

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._running = True
            self._task = asyncio.ensure_future(self._run())
            # Handle reminders that came due while the app was closed.
            asyncio.ensure_future(self.recover_missed())

    async def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def notify_changed(self) -> None:
        """Call after add/cancel/snooze so the scanner wakes immediately."""
        self._wake.set()

    # -- loop ---------------------------------------------------------------
    async def _run(self) -> None:
        while self._running:
            try:
                due = self.store.list(active_only=True)
                now = self.clock.now()
                if not due:
                    sleep_for = MAX_POLL_SECONDS
                else:
                    next_ts = min(r.due_at for r in due)
                    sleep_for = max(0.0, min(next_ts - now, MAX_POLL_SECONDS))
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=sleep_for)
                    continue  # woke early: recompute
                except asyncio.TimeoutError:
                    pass
                await self.fire_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1.0)

    async def fire_due(self) -> list[Reminder]:
        fired = []
        for rem in self.store.due_now():
            if rem.status == "snoozed":
                self.store._update(rem.id, status="scheduled")  # wake from snooze
                rem = self.store.get(rem.id)
            note = ""
            if rem.attempts > 0 and rem.follow_up:
                note = f"Follow-up {rem.attempts + 1} of {rem.follow_up['max']}:"
            elif rem.recurring:
                note = "Recurring reminder:"
            await self._deliver(rem, note)
            fired.append(rem)
        return fired

    async def _deliver(self, rem: Reminder, note: str) -> None:
        waiting_ack = rem.follow_up is not None
        try:
            await self.on_due(rem, note)
        finally:
            fresh = self.store.get(rem.id) or rem
            if waiting_ack:
                self.store.mark_fired(fresh, waiting_ack=True)
                refreshed = self.store.get(rem.id)
                if refreshed is not None and not self.store.follow_up_due(refreshed):
                    # Follow-up budget exhausted: recurring -> next occurrence,
                    # one-shot -> auto-finish (honest: marked done, not acked).
                    self.store.exhaust_follow_up(refreshed)
            else:
                self.store.mark_fired(fresh, waiting_ack=False)
            self.deliveries.append({
                "id": rem.id, "text": rem.text, "ts": self.clock.now(),
                "attempts": (self.store.get(rem.id) or rem).attempts,
            })

    # -- startup recovery -------------------------------------------------------
    async def recover_missed(self) -> list[dict]:
        """Reminders that came due while the app was closed."""
        now = self.clock.now()
        handled = []
        for rem in self.store.due_now(now):
            if rem.status == "snoozed":
                continue
            self.store.mark_missed(rem)
            info = {"id": rem.id, "text": rem.text, "recurring": rem.recurring, "replayed": False}
            if self.missed_replay:
                note = "(This reminder came due while Aurora was not running.)"
                await self._deliver(rem, note)
                info["replayed"] = True
            if rem.recurring:
                self.store.reschedule_recurring(rem, after=now)
            handled.append(info)
        if handled:
            self.notify_changed()
        return handled
