"""Memory retrieval for prompt injection.

The single recall path used by every consumer (chat prompt, proactive
topics, reminder context). Combines three slots — pinned facts, recent
facts, and query-relevant facts (FTS5 fused with optional embedding
cosine via Reciprocal Rank Fusion, the same merge Shikigami uses for
BM25+vector) — under one cap so the prompt never bloats.
"""

from __future__ import annotations

from ..clock import Clock
from ..providers.embeddings import EmbeddingClient
from ..storage import Database
from .store import Fact, FactStore


def rrf_merge(rankings: list[list[str]], k: int = 60) -> list[str]:
    """Reciprocal Rank Fusion over ranked id lists."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for pos, fact_id in enumerate(ranking):
            scores[fact_id] = scores.get(fact_id, 0.0) + 1.0 / (k + pos + 1)
    return sorted(scores, key=scores.get, reverse=True)


class MemoryRetriever:
    def __init__(self, db: Database, store: FactStore, clock: Clock,
                 embeddings: EmbeddingClient | None = None,
                 max_facts: int = 12, recent_days: int = 3) -> None:
        self.db = db
        self.store = store
        self.clock = clock
        self.embeddings = embeddings
        self.max_facts = max_facts
        self.recent_days = recent_days

    async def _semantic_rank(self, query: str, candidates: list[Fact]) -> list[str]:
        """Rank candidate ids by cosine similarity to the query when an
        embedding client is configured; empty list on any failure."""
        if self.embeddings is None or not candidates or not query.strip():
            return []
        try:
            texts = [query] + [f.content for f in candidates]
            vectors = await self.embeddings.embed(texts)
        except Exception:
            return []
        if not vectors or vectors[0] is None:
            return []
        qvec = vectors[0]
        scored: list[tuple[float, str]] = []
        for i, fact in enumerate(candidates):
            vec = vectors[i + 1]
            if vec is None:
                continue
            scored.append((self.embeddings.cosine(qvec, vec), fact.id))
        scored.sort(reverse=True)
        return [fact_id for _, fact_id in scored]

    async def retrieve(self, query: str, subject: str = "owner", limit: int | None = None) -> list[Fact]:
        limit = limit or self.max_facts
        picked: dict[str, Fact] = {}

        # Slot 1: pinned facts always travel.
        for f in self.store.active_facts(subject):
            if f.pinned:
                picked[f.id] = f
        # Slot 2: recent facts (last `recent_days` days).
        for f in self.store.recent_facts(days=self.recent_days, limit=5):
            picked.setdefault(f.id, f)

        budget = max(0, limit - len(picked))
        if budget == 0:
            return list(picked.values())[:limit]

        # Slot 3: query-relevant facts, FTS5 + lexical + optional vector, RRF-merged.
        fts_hits = self.store.facts_fts(query, subject=subject, limit=10)
        lexical_hits = self.store.facts_about(query, subject=subject, limit=10)
        ranked_lists: list[list[str]] = [[f.id for f in fts_hits], [f.id for f in lexical_hits]]
        seen = {f.id for f in fts_hits}
        vec_candidates = list(fts_hits) + [f for f in lexical_hits if f.id not in seen]
        vec_ranking = await self._semantic_rank(query, vec_candidates)
        if vec_ranking:
            ranked_lists.append(vec_ranking)

        existing = {f.id: f for f in fts_hits + lexical_hits}
        for fact_id in rrf_merge(ranked_lists):
            if len(picked) >= limit:
                break
            fact = existing.get(fact_id) or self.store.get_fact(fact_id)
            if fact is not None and fact.active:
                picked.setdefault(fact_id, fact)

        return list(picked.values())[:limit]

    async def facts_block(self, query: str, subject: str = "owner") -> str:
        """Render the remembered-facts block for the system prompt."""
        facts = await self.retrieve(query, subject=subject)
        if not facts:
            return ""
        lines = []
        for f in facts:
            marker = " (pinned)" if f.pinned else ""
            lines.append(f"- {f.content}{marker}")
        return f"Things you remember about {subject}:\n" + "\n".join(lines)
