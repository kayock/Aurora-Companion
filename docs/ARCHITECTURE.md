# Aurora architecture

## Decision: neither upstream became the literal foundation

Luna (~87k lines) is threaded through with a terminal UI, games, plugins and adult-content
modules; Shikigami carries a 4k-line settings surface, Electron packaging and a Docker-based
task harness. Aurora is a **new, single codebase** that re-derives the best *algorithms* of
both and adapts their proven patterns, with attribution in NOTICE.md. Both upstreams were
inspected in depth before the decision; the reuse evaluation for each is in
FEATURE_COMPARISON.md.

## The "one X" guarantees (all structurally enforced)

| Requirement | How Aurora enforces it |
|---|---|
| One conversation manager | `aurora/conversation/manager.py` — every assistant-visible turn (chat, slash commands, reminder deliveries, proactive initiations) flows through `ConversationManager`; reminders and the proactive scheduler *request* turns, they never call the model themselves |
| One memory architecture | `aurora/memory/` + the `facts` table — the only fact store; retrieval, extraction, consolidation and the dashboard all read/write it |
| One personality engine | `aurora/personality/` — persona (seed+evolved+anchor), mood (energy/warmth), affinity (tiers); exactly one `PersonaEvolution` instance exists (wired through `CompanionApp`) |
| One proactive scheduler | `aurora/proactive/scheduler.py` — the only code path that initiates messages; all initiations journaled in `proactive_log` |
| One reminder system | `aurora/reminders/` — one SQLite table, one scheduler loop, all delivery through the conversation manager |
| One voice system | `aurora/voice/` — one `VoiceService`; TTS providers: none/kokoro/edge/**kayock** (existing OpenAI-compatible speech adapter with live voice-id discovery), STT: none/whisper/**kayock** (existing Faster-Whisper service); speech output hooks the conversation manager's post-turn, input shares the same history |
| Modular providers | `aurora/providers/` — `ChatProvider` seam; OpenAI-compatible (LM Studio/llama.cpp/Ollama/any `/v1`), mock for tests; capability flags (`supports_tools()`) degrade gracefully |
| Separation of services and inference | background services (reminder scanner, proactive loop, consolidation) are plain asyncio loops over the store; they call the model only through injected providers and keep working when it is down |
| Compute yielding | `aurora/resources/` — a read-only `ResourceCoordinator` (generic GET probe for an external GPU traffic light, disabled until its protocol is verified) and a `ComputeGuard` that gates all nonessential background AI on model health (+loaded-model check), foreground grace and the light's busy state; foreground chat never consults the guard |

## Process model

FastAPI (uvicorn) serves REST + SSE. All services are asyncio tasks in one process; SQLite
(WAL) is the single store; one `Clock` abstraction makes every time-dependent service
virtual-time-testable.

## Data model (`data/aurora.db`)

- `messages` — the one conversation history (role, content, person, source user/chat/proactive/reminder/system)
- `facts` — long-term memory: content, subject (per person), category, weight, trust,
  pinned, archived+reason, merged_into, superseded_by/at (Luna's retire-don't-delete chain),
  valid_from/valid_to (bi-temporal), evidence (JSON pointers to message ids + quotes),
  versions, expires_at, optional embedding BLOB
- `facts_fts` — FTS5 index kept in sync by triggers
- `forgotten` + tombstones — "forget it" refusal list
- `reminders` — id, text, due_at, repeat (interval/daily/weekly), follow_up spec, status
  (scheduled/delivered/waiting_ack/snoozed/done/cancelled/missed), ack, attempts, snooze_count, project, person
- `proactive_log` — every companion-initiated message (dedup + audit)
- `personality_log` — persona evolution changelog (before/after, summary, reason, baseline flag)
- `lessons`, `day_summaries` — consolidation output
- `people`, `mood_state`, `embeddings` (cache), `meta` (counters, cursors, learned sleep window, persona state)

## Turn flow (user message)

1. persist user message; bump counters; wake activity trackers (proactive, consolidation, reminder scanner)
2. slash commands resolved locally (`/remind`, `/reminders`, `/remember`, `/forget`, `/facts`, `/help`)
3. explicit "remember …" (anchored regex) stores a fact immediately with evidence
4. prompt builder composes: persona(+evolved+anchor) → mood/affinity → recalled facts
   (pinned ∪ recent ∪ RRF(FTS5, lexical, optional vectors)) → lessons → time → upcoming
   reminders → people context → rolling summary → tools manifest
5. provider call (streaming deltas → SSE); tool loop only if `supports_tools()`; degrade
   to no-tools on server errors; 429/5xx retried with backoff
6. assistant message persisted, broadcast; **post-turn (fire-and-forget)**: mood appraisal
   (model or transparent lexical fallback), affinity increment + periodic adjustment,
   extraction every N user turns (evidence-validated ops), evolution cadence check,
   rolling-summary maintenance
7. model unreachable → honest visible error message; nothing invented

## Proactive pipeline (all paths)

`ProactiveScheduler.tick()` → gates (enabled → focus period → quiet hours (config ∧ learned
sleep window) → min silence → global daily cap → no-response backoff) → per-kind gates
(daily cap, urgency, cooldown `min_interval * (1 - urgency*0.85)`, 24 h topic-ref dedup) →
topic selection (memory resurfacing / user life / ai self / icebreaker) →
`ConversationManager.send_proactive()` (proactive frame + topic in system prompt) →
persisted as `source='proactive'` + journaled. Reminder deliveries use the same manager
(`deliver_reminder`): model-phrased when up, plain "⏰" text when down; follow-ups re-fire
until acknowledged or exhausted; recurring ones reschedule to their next occurrence.

## Testing strategy

`MockProvider` (deterministic scripted LLM) + virtual `Clock` + `httpx.MockTransport` make
the whole orchestration deterministic (199 tests, including the Kayock voice clients,
resource deferral and LAN auth). A real-model integration path runs the same provider code
against any OpenAI-compatible endpoint (see TEST_RESULTS.md); the physical layers
(hardware, LAN, the traffic light's real protocol) are verified on-site via
docs/PORTABLE_DEPLOY.md §7 — never claimed from the sandbox.
