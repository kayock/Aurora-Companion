# Notices and attribution

Aurora is a new unified application that combines the strongest capabilities of two
AGPL-3.0 projects. It does not modify either upstream repository.

## Luna Local Agent
- Upstream: https://github.com/fswolf/luna-local-agent
- Copyright (C) 2026 fswolf
- License: AGPL-3.0-or-later (versions released before November 20, 2026 were MIT and remain MIT)
- What Aurora derives from it (see docs/FEATURE_COMPARISON.md): the single-SQLite-store
  design with FTS5 fact indexing, retire-don't-delete supersession, bi-temporal validity,
  forgotten-fact tombstones, the "model extracts words / Python does date math" reminder
  principle, the two-dial (energy/warmth) mood engine with hour baselines, restart fade and
  diminishing praise returns, per-person memory separation, and the sleep-until-due reminder
  scanner with a wake event.

## Shikigami Protocol
- Upstream: https://github.com/Shikigami-Lab/Shikigami-Protocol
- License: AGPL-3.0
- What Aurora derives from it (see docs/FEATURE_COMPARISON.md): the FastAPI + web dashboard
  shape, evidence-validated memory extraction with a per-turn cursor, fact records with
  weight/category/evidence/versions/archive-merge soft deletes, hybrid retrieval fused by
  Reciprocal Rank Fusion, rolling conversation summaries, the emotion/energy/affinity state
  engines and relationship tiers, the autonomous-self-expression (ASE) proactive pipeline
  with urgency gates and the cooldown formula `min_interval * (1 - urgency * 0.85)`, and the
  persona-evolution engine (core anchor, evidence gating, baseline review, changelog, rollback).

## License
Aurora itself is licensed under the GNU Affero General Public License v3.0 or any later
version (AGPL-3.0-or-later), preserving the licensing requirements of both upstream
projects. See LICENSE for the full text. Third-party Python dependencies keep their own
licenses (BSD/MIT/Apache-class; see requirements.txt).
