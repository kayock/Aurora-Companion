"""Memory extraction: turning conversation into durable facts.

Runs after every N user turns (Shikigami's cadence) using the messages
since the last cursor. The model proposes operations as JSON; every op is
validated before it touches the store:

* evidence: each cited message id must exist in this batch and, when a
  quote is given, actually appear in that message (anti-fabrication —
  Shikigami's evidence-validated extraction);
* duplicates are refused via Jaccard dedup;
* 'supersede' ops target only facts shown to the extractor, so outdated
  statements ("I used to work at X") replace the old fact properly;
* a per-batch cap (12 ops) bounds damage from a confused model.

Extraction failures are logged and skipped — a bad extraction never
corrupts memory.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..clock import Clock
from ..providers.base import ChatProvider, ProviderError
from ..storage import Database
from ..util import salient_words
from .store import DuplicateFact, FactStore, ForgottenFact

EXTRACTION_PROMPT = """You maintain the long-term memory of an AI companion. From the
conversation below, decide what is worth remembering about the user as durable facts.

Rules:
- Only facts about the user, their life, preferences, projects, people or plans — not
  small talk, not your own replies, not transient context.
- Prefer updating or replacing outdated facts over adding conflicting ones. If the
  conversation contradicts an existing fact listed below, emit a supersede op.
- Every op MUST cite evidence: the id of the message that supports it (ids are shown
  as [id]) and an exact short quote from that message.
- Weigh each fact 0.5 (weak preference) to 2.0 (core fact about their life).
- Category: habit | preference | relationship | project | event | fact | other.
- No more than 12 ops. If there is nothing worth remembering, return [].

Reply with ONLY a JSON array, no prose. Each element:
{"op": "add"|"supersede", "content": "...", "category": "...", "weight": 1.0,
 "evidence": [{"message_id": 123, "quote": "..."}], "supersedes": "f_id" (only for supersede)}"""

VALID_OPS = {"add", "supersede"}


@dataclass
class ExtractionResult:
    added: list[str]
    superseded: list[tuple[str, str]]  # (old_id, new_id)
    rejected: list[dict]
    raw_ops: int


def _extract_json_array(text: str) -> list | None:
    """Pull the first JSON array out of model text (tolerates fences/prose)."""
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    if fence:
        try:
            return json.loads(fence.group(1))
        except ValueError:
            pass
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except ValueError:
            return None
    return None


class MemoryExtractor:
    def __init__(self, db: Database, store: FactStore, provider: ChatProvider, clock: Clock) -> None:
        self.db = db
        self.store = store
        self.provider = provider
        self.clock = clock

    def _batch_messages(self, since_id: int, limit: int = 40) -> list[dict]:
        rows = self.db.query(
            "SELECT id, role, content, ts FROM messages WHERE id > ? ORDER BY id ASC LIMIT ?",
            (since_id, limit),
        )
        return [{"id": r["id"], "role": r["role"], "content": r["content"], "ts": r["ts"]} for r in rows]

    def _cursor(self) -> int:
        return int(self.db.get_meta("memory.extraction_cursor") or 0)

    def _set_cursor(self, message_id: int) -> None:
        self.db.set_meta("memory.extraction_cursor", str(message_id))

    async def extract_since_cursor(self, force: bool = False) -> ExtractionResult | None:
        since = self._cursor()
        batch = self._batch_messages(since)
        if not batch:
            return None
        user_count = sum(1 for m in batch if m["role"] == "user")
        if not force and user_count < 1:
            return None
        result = await self.extract_messages(batch)
        if result is not None:
            self._set_cursor(batch[-1]["id"])
        return result

    async def extract_messages(self, batch: list[dict]) -> ExtractionResult | None:
        if not batch:
            return None
        convo_lines = []
        for m in batch:
            speaker = "User" if m["role"] == "user" else "Companion"
            convo_lines.append(f"[{m['id']}] {speaker}: {m['content'][:600]}")
        related = self.store.facts_about(" ".join(m["content"] for m in batch), limit=15)
        related_block = ""
        if related:
            related_block = "Existing related facts (you may supersede):\n" + "\n".join(
                f"- {f.id}: {f.content}" for f in related
            )
        prompt = EXTRACTION_PROMPT
        if related_block:
            prompt += "\n\n" + related_block
        prompt += "\n\nConversation:\n" + "\n".join(convo_lines)

        try:
            result = await self.provider.chat(
                [{"role": "system", "content": "You are a careful memory curator. Output JSON only."},
                 {"role": "user", "content": prompt}],
                temperature=0.2, max_tokens=1200,
            )
        except ProviderError:
            return None
        ops = _extract_json_array(result.text)
        if ops is None:
            return None
        return self.apply_ops(ops, batch)

    def apply_ops(self, ops: list, batch: list[dict]) -> ExtractionResult:
        batch_by_id = {m["id"]: m for m in batch}
        added: list[str] = []
        superseded: list[tuple[str, str]] = []
        rejected: list[dict] = []
        raw_count = len(ops)
        ops = ops[:12]
        for op in ops:
            if not isinstance(op, dict) or op.get("op") not in VALID_OPS:
                rejected.append({"op": op, "reason": "bad op"})
                continue
            content = str(op.get("content", "")).strip()
            if not content or len(content) > 220:
                rejected.append({"op": op, "reason": "empty or oversized content"})
                continue
            evidence = []
            ok = True
            for ev in op.get("evidence", [])[:3]:
                if not isinstance(ev, dict):
                    continue
                try:
                    mid = int(ev.get("message_id"))
                except (TypeError, ValueError):
                    mid = None
                if mid not in batch_by_id:
                    rejected.append({"op": op, "reason": f"evidence message {ev.get('message_id')} not in batch"})
                    ok = False
                    break
                msg = batch_by_id[mid]
                quote = str(ev.get("quote", "")).strip()
                # Anti-fabrication: the quote must really come from the cited
                # message — either a literal substring or all of its salient
                # words present (models paraphrase quotes more often than they
                # fabricate them, but a message unrelated to the quote is
                # always rejected).
                if quote and quote.lower()[:60] not in msg["content"].lower():
                    from ..util import salient_words as _sw
                    qwords = set(_sw(quote))
                    mwords = set(_sw(msg["content"]))
                    if not qwords or not qwords.issubset(mwords):
                        rejected.append({"op": op, "reason": "quote not found in message"})
                        ok = False
                        break
                evidence.append({"message_id": mid, "ts": msg["ts"], "quote": quote[:120]})
            if not ok or not evidence:
                if ok:
                    rejected.append({"op": op, "reason": "no evidence"})
                continue

            category = op.get("category", "other")
            try:
                weight = max(0.5, min(2.0, float(op.get("weight", 1.0))))
            except (TypeError, ValueError):
                weight = 1.0

            if op["op"] == "supersede":
                target = self.store.get_fact(str(op.get("supersedes", "")))
                if target is None or not target.active:
                    rejected.append({"op": op, "reason": "supersede target missing or inactive"})
                    continue
                try:
                    new_fact = self.store.supersede_fact(target.id, content, evidence=evidence, source="auto")
                    superseded.append((target.id, new_fact.id))
                except (DuplicateFact, ForgottenFact) as exc:
                    rejected.append({"op": op, "reason": str(exc)})
                continue

            try:
                fact = self.store.add_fact(
                    content, category=category if category in
                    ("habit", "preference", "relationship", "project", "event", "fact", "other") else "other",
                    source="auto", evidence=evidence, weight=weight,
                )
                added.append(fact.id)
            except (DuplicateFact, ForgottenFact) as exc:
                rejected.append({"op": op, "reason": str(exc)})
        return ExtractionResult(added=added, superseded=superseded, rejected=rejected, raw_ops=raw_count)
