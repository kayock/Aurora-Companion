"""Topic sources for proactive participation.

Adapted from Shikigami Protocol's topic-source registry: each source
offers candidate topics ({source, ref, summary}); the scheduler picks one
that has not been surfaced recently (dedup by ref against the proactive
log), and the conversation manager renders it with the model.

Aurora ships four sources:
* memory_resurfacing — an old shared memory worth revisiting
* user_life — a high-weight recent fact, followed up conversationally
* ai_self — what the companion has been "thinking about" (mood + last thought)
* icebreaker — small local pool, honest fallback that needs no model
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from ..clock import Clock
from ..memory.store import FactStore
from ..personality.mood import MoodEngine
from ..storage import Database

ICEBREAKERS = [
    "Have you been able to spend time on anything you enjoy lately?",
    "Is there anything you're looking forward to this week?",
    "How has your energy been today?",
    "Anything interesting you've read or watched recently?",
]


@dataclass
class TopicCandidate:
    source: str
    ref: str          # dedup key (fact id / fixed ref)
    summary: str
    angle: str = ""


class TopicSource:
    def candidates(self, ctx: dict) -> list[TopicCandidate]:
        raise NotImplementedError


class MemoryResurfacing(TopicSource):
    def __init__(self, store: FactStore, older_than_days: float = 14) -> None:
        self.store = store
        self.older_than_days = older_than_days

    def candidates(self, ctx: dict) -> list[TopicCandidate]:
        out = []
        for fact in self.store.old_facts(self.older_than_days):
            if fact.weight < 0.8:
                continue
            out.append(TopicCandidate(
                source="memory_resurfacing", ref=fact.id,
                summary=f"Bring up this older shared memory: \"{fact.content}\"",
                angle="do you remember / how did that turn out",
            ))
        return out


class UserLife(TopicSource):
    def __init__(self, store: FactStore) -> None:
        self.store = store

    def candidates(self, ctx: dict) -> list[TopicCandidate]:
        facts = [f for f in self.store.active_facts()
                 if f.category in ("project", "event", "habit", "preference") and f.weight >= 1.0]
        out = []
        for fact in facts[:6]:
            out.append(TopicCandidate(
                source="user_life", ref=fact.id,
                summary=f"Follow up on: \"{fact.content}\"",
                angle="how is it going / any progress",
            ))
        return out


class AiSelf(TopicSource):
    def __init__(self, db: Database, mood: MoodEngine | None = None) -> None:
        self.db = db
        self.mood = mood

    def candidates(self, ctx: dict) -> list[TopicCandidate]:
        state = self.db.get_meta("reflection.last_thought") or ""
        if not state:
            return []
        return [TopicCandidate(
            source="ai_self", ref="self:" + state[:40],
            summary=f"Share what you've been musing about: \"{state[:140]}\"",
            angle="a thought to share",
        )]


class Icebreaker(TopicSource):
    def candidates(self, ctx: dict) -> list[TopicCandidate]:
        pick = random.choice(ICEBREAKERS)
        return [TopicCandidate(source="icebreaker", ref="ice:" + pick[:30], summary=pick)]


def get_candidates(sources: list[TopicSource], ctx: dict) -> list[TopicCandidate]:
    out: list[TopicCandidate] = []
    for source in sources:
        try:
            out.extend(source.candidates(ctx))
        except Exception:
            continue
    return out
