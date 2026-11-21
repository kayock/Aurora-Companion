"""Conversation prompt builder — one coherent prompt per turn.

Segments compose in a fixed order (persona → mood → memory → time →
tools → context), adapting both upstreams' pipelines: Shikigami's
segmented system prompt and Luna's stable/live split. The whole system
message is stable except the live sections, which keeps local-model
prompt caching effective.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..personality.mood import MoodEngine
from ..personality.persona import Persona
from ..storage import Database

if TYPE_CHECKING:  # pragma: no cover
    from ..reminders.store import ReminderStore


def time_context(now_epoch: float) -> str:
    dt = datetime.fromtimestamp(now_epoch, tz=timezone.utc)
    return (f"Current time (UTC): {dt.strftime('%A, %Y-%m-%d %H:%M')}. "
            "Use it naturally; never claim a different date or time.")


class PromptBuilder:
    def __init__(self, db: Database, persona: Persona, mood: MoodEngine | None,
                 retriever, reminder_store: "ReminderStore | None", user_name: str,
                 people_registry=None) -> None:
        self.db = db
        self.persona = persona
        self.mood = mood
        self.retriever = retriever
        self.reminders = reminder_store
        self.user_name = user_name
        self.people = people_registry

    async def build_system(
        self,
        now_epoch: float,
        query: str,
        subject: str = "owner",
        extra_blocks: list[str] | None = None,
        proactive_context: str | None = None,
    ) -> str:
        blocks: list[str] = []

        # 1. Identity (stable).
        blocks.append(self.persona.prompt_block(self.user_name))

        # 2. Mood & relationship (live).
        if self.mood is not None:
            mood_block = self.mood.prompt_block()
            if mood_block:
                blocks.append(mood_block)

        # 3. Memory (live, query-shaped).
        if self.retriever is not None:
            facts = await self.retriever.facts_block(query, subject=subject)
            if facts:
                blocks.append(facts)
            lessons = self.db.query("SELECT text FROM lessons ORDER BY created_at DESC LIMIT 5")
            if lessons:
                blocks.append("Lessons you have learned from past mistakes (follow them):\n" +
                              "\n".join(f"- {r['text']}" for r in lessons))

        # 4. Time (live).
        blocks.append(time_context(now_epoch))

        # 5. Companion behavior (stable) — application-level guardrails so the
        # model does not invent reminders, countdown-nag, or over-correct ASR.
        blocks.append(
            "Conversation guardrails:\n"
            "- Only call set_reminder when the user clearly asks to be reminded "
            "or to set a timer. Never schedule greetings, check-ins, breaks, or "
            "your own suggestions as reminders.\n"
            "- Do not announce, count down, or repeatedly mention upcoming "
            "reminders during ordinary chat. Mention them only if the user asks "
            "about reminders, or when this turn is an actual reminder delivery.\n"
            "- Work-break and check-in ideas are spoken once as conversation — "
            "not turned into scheduled tasks unless the user asks.\n"
            "- Speech recognition can mishear names and greetings (e.g. "
            "'Hey, Rorro' for 'Hey, Aurora'). Respond naturally; do not treat "
            "mishearings as mistakes the user must correct.\n"
            "- Keep proactive tone varied and brief; avoid repeating the same "
            "nudge in consecutive turns."
        )

        # 6. Upcoming reminders (live) — internal reference for tools only.
        # Deliberately omit relative countdowns ("in 20 minutes") so the model
        # is not nudged into premature announcement during unrelated chat.
        if self.reminders is not None:
            upcoming = [r for r in self.reminders.list(active_only=True, person=subject)][:5]
            if upcoming:
                from ..timeparse import describe_repeat
                lines = []
                for r in upcoming:
                    rep = f" ({describe_repeat(r.repeat)})" if r.repeat else ""
                    lines.append(f"- [{r.id}] \"{r.text}\"{rep}")
                blocks.append(
                    "Active reminders (internal reference — do not recite or "
                    "count down unless the user asks about their reminders):\n"
                    + "\n".join(lines)
                )

        # 7. People context.
        if self.people is not None:
            block = self.people.context_block(subject)
            if block:
                blocks.append(block)

        # 8. Proactive frame (when this turn is companion-initiated).
        if proactive_context:
            blocks.append(proactive_context)

        # 9. Caller-supplied extras (e.g. screen descriptions).
        for extra in extra_blocks or []:
            blocks.append(extra)

        return "\n\n".join(blocks)
