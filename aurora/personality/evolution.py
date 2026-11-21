"""Persona evolution engine.

Evidence-gated identity growth, adapted from Shikigami Protocol's
persona_evolution with the same safety rails:

* Fires only after `every_n_user_turns` user turns since the last
  evolution (never on a timer), one run at a time.
* The core anchor (extracted on the first run) is included in the rewrite
  prompt; a run that reports `anchor_preserved: false` is discarded.
* Original section headings must survive verbatim; over-length output is
  rejected (soft cap 1.5x the seed, hard 1.7x).
* At most a few changes per round; every Nth run is a *baseline review*
  that rebuilds from the original seed keeping only evidence-backed
  traits (anti-drift).
* Every run is journaled with before/after text, summary and reason;
  rollback is one click.

Requires a background model; without one evolution is skipped (reported
honestly) rather than faked.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..clock import Clock
from ..providers.base import ChatProvider, ProviderError
from ..storage import Database
from .persona import Persona

EVOLVE_PROMPT = """You are guiding the slow personality evolution of an AI companion.

ORIGINAL PERSONA (the seed, immutable):
{original}

CURRENT EVOLVED PERSONA:
{evolved}

CORE ANCHOR — traits that must never change:
{anchor}

RECENT FACTS learned about the user and shared experiences (evidence):
{facts}

RECENT CONVERSATION EXCERPT:
{conversation}

RELATIONSHIP STATE: {affinity}

{mode}

Rewrite rules:
- Make at most 2 small changes this round. A round with too little evidence should
  change nothing (return the current evolved persona unchanged).
- Only evolve traits that are backed by the evidence: real preferences, real shared
  experiences, real repeated patterns.
- Never soften or blunt the anchor traits (anti-drift). Never add invented backstory,
  events or skills the companion does not have. Never absorb the user's traits.
- Preserve every section heading of the persona exactly as written.
- Keep the persona under {soft_cap} characters.

Reply with ONLY a JSON object:
{{"base_prompt": "...", "style": "...", "change_summary": "one sentence",
 "reason": "evidence-based justification", "anchor_preserved": true}}"""

BASELINE_MODE = """BASELINE REVIEW: rebuild from the ORIGINAL persona, keeping only changes that
have lasting evidence from the facts below. Drop changes that never gained evidence."""

REGULAR_MODE = "REGULAR EVOLUTION: start from the CURRENT evolved persona and apply the small changes."


def _parse_json_object(text: str) -> dict | None:
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidates = [fence.group(1) if fence else None]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for cand in candidates:
        if not cand:
            continue
        try:
            value = json.loads(cand)
            if isinstance(value, dict):
                return value
        except ValueError:
            continue
    return None


def _section_headings(text: str) -> list[str]:
    """Headings of the persona document (## lines, 【…】, [Brackets])."""
    heads = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("##") and len(line) > 2:
            heads.append(line)
        elif line.startswith("【") and "】" in line:
            heads.append(line)
        elif line.startswith("[") and line.endswith("]") and len(line) < 60:
            heads.append(line)
    return heads


@dataclass
class EvolutionResult:
    fired: bool
    version: int | None = None
    change_summary: str = ""
    reason: str = ""
    skipped_reason: str = ""
    baseline_review: bool = False


class PersonaEvolution:
    def __init__(self, db: Database, persona: Persona, provider: ChatProvider | None,
                 clock: Clock, affinity_label: str = "", config: dict | None = None) -> None:
        self.db = db
        self.persona = persona
        self.provider = provider
        self.clock = clock
        self.affinity_label = affinity_label
        cfg = config or {}
        self.every_n_turns = int(cfg.get("every_n_user_turns", 40))
        self.baseline_every = int(cfg.get("baseline_review_every", 3))
        self._running = False
        self.last_result: EvolutionResult | None = None

    def turns_since_last(self, user_turn_count: int) -> int:
        # user_turn_count is the total user turns this persona has seen.
        return user_turn_count - int(self.db.get_meta("persona.turns_at_last_evolution") or 0)

    def maybe_evolve(self, user_turn_count: int) -> bool:
        """Check trigger conditions; returns True if an evolution run starts."""
        if self.every_n_turns <= 0 or self._running:
            return False
        if self.turns_since_last(user_turn_count) < self.every_n_turns:
            return False
        return True

    async def evolve(self, facts_text: str, conversation_text: str, force: bool = False,
                     baseline_review: bool | None = None) -> EvolutionResult:
        if self._running:
            return EvolutionResult(fired=False, skipped_reason="already running")
        if self.provider is None:
            self.last_result = EvolutionResult(fired=False, skipped_reason="no background model")
            return self.last_result
        self._running = True
        try:
            s = self.persona.state
            count = s.evolution_count + 1
            if baseline_review is None:
                baseline_review = self.baseline_every > 0 and count % self.baseline_every == 0
            original = s.base_prompt
            evolved = s.effective_prompt()
            soft_cap = max(1500, int(len(original) * 1.5))
            anchor = s.core_anchor
            if not anchor:
                anchor = await self._extract_anchor(original)
                if not anchor:
                    self.last_result = EvolutionResult(fired=False, skipped_reason="anchor extraction failed")
                    return self.last_result

            prompt = EVOLVE_PROMPT.format(
                original=original, evolved=evolved, anchor=anchor,
                facts=facts_text or "(no facts yet)", conversation=conversation_text[:4000] or "(none)",
                affinity=self.affinity_label or "new acquaintance",
                mode=BASELINE_MODE if baseline_review else REGULAR_MODE,
                soft_cap=soft_cap,
            )
            try:
                result = await self.provider.chat(
                    [{"role": "user", "content": prompt}], temperature=0.4, max_tokens=2000
                )
            except ProviderError as exc:
                self.last_result = EvolutionResult(fired=False, skipped_reason=f"model error: {exc}")
                return self.last_result
            parsed = _parse_json_object(result.text)
            if parsed is None:
                self.last_result = EvolutionResult(fired=False, skipped_reason="unparseable evolution output")
                return self.last_result
            new_prompt = str(parsed.get("base_prompt", "")).strip()
            new_style = str(parsed.get("style", "")).strip()
            summary = str(parsed.get("change_summary", "")).strip()
            reason = str(parsed.get("reason", "")).strip()
            anchor_preserved = parsed.get("anchor_preserved") is not False

            if not new_prompt:
                self.last_result = EvolutionResult(fired=False, skipped_reason="empty persona output")
                return self.last_result
            if not anchor_preserved:
                self.last_result = EvolutionResult(fired=False, skipped_reason="model reported anchor broken")
                return self.last_result

            orig_heads = _section_headings(original)
            new_heads = _section_headings(new_prompt)
            missing_heads = [h for h in orig_heads if h not in new_heads]
            if missing_heads and orig_heads:
                # One corrective retry: real models often drop headings when
                # rewriting; ask once to restore them before giving up.
                fix_prompt = (
                    "Your rewrite dropped required section headings: "
                    + "; ".join(missing_heads)
                    + ".\nRewrite the persona again keeping ALL of these headings "
                    + "exactly as written, in place:\n" + "\n".join(orig_heads)
                    + "\n\nCurrent draft:\n" + new_prompt[:3000]
                    + '\n\nReply with ONLY the same JSON object shape: {"base_prompt": "...", '
                    '"style": "...", "change_summary": "...", "reason": "...", "anchor_preserved": true}'
                )
                try:
                    retry = await self.provider.chat(
                        [{"role": "user", "content": fix_prompt}], temperature=0.3, max_tokens=2000
                    )
                    parsed2 = _parse_json_object(retry.text)
                except ProviderError:
                    parsed2 = None
                if parsed2 and parsed2.get("base_prompt"):
                    new_prompt = str(parsed2["base_prompt"]).strip()
                    new_style = str(parsed2.get("style", "")).strip() or new_style
                    missing_heads = [h for h in orig_heads if h not in _section_headings(new_prompt)]
            if orig_heads and missing_heads:
                self.last_result = EvolutionResult(
                    fired=False, skipped_reason="section headings not preserved after retry")
                return self.last_result
            hard_cap = int(soft_cap * 1.15)
            if len(new_prompt) > hard_cap:
                self.last_result = EvolutionResult(fired=False, skipped_reason="over hard length cap")
                return self.last_result
            unchanged = new_prompt == evolved and new_style == s.effective_style()
            version = self.persona.apply_evolution(
                new_prompt, new_style, summary, reason, anchor, baseline_review
            )
            self.db.set_meta("persona.turns_at_last_evolution", str(self.db.get_meta("stats.user_turns") or 0))
            self.last_result = EvolutionResult(
                fired=not unchanged, version=version, change_summary=summary, reason=reason,
                baseline_review=baseline_review,
                skipped_reason="no change" if unchanged else "",
            )
            return self.last_result
        finally:
            self._running = False

    async def _extract_anchor(self, original: str) -> str:
        prompt = (
            "Extract 3-6 immutable core traits of this AI companion persona, as a short "
            "semicolon-separated list. These traits define who the companion fundamentally is "
            "and must survive all future personality evolution.\n\nPERSONA:\n" + original +
            '\n\nReply with ONLY JSON: {"core_anchor": "trait; trait; trait", "rationale": "..."}'
        )
        try:
            result = await self.provider.chat([{"role": "user", "content": prompt}],
                                              temperature=0.3, max_tokens=300)
        except ProviderError:
            return ""
        parsed = _parse_json_object(result.text)
        if not parsed or not parsed.get("core_anchor"):
            return ""
        anchor = str(parsed["core_anchor"])[:300]
        self.persona.state.core_anchor = anchor
        self.persona._save()
        return anchor
