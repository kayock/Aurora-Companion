# Feature comparison — what Aurora kept from each upstream

Legend: ✅ kept & unified · 🔶 partially kept / simplified · ❌ deliberately not carried

## From Luna Local Agent

| Luna capability | Aurora | Notes |
|---|---|---|
| SQLite fact store w/ FTS5, supersession, bi-temporal validity, trust, tombstones | ✅ | `aurora/memory/store.py`, extended with evidence pointers, versions, archive/merge |
| "Model extracts words, Python does date math" | ✅ | `aurora/timeparse.py` (compact reimplementation of the principle) |
| Reminder scanner: sleep-until-due + wake event; missed-on-startup delivery | ✅ | `aurora/reminders/scheduler.py`; extended with recurrence + follow-up-until-ack (Luna had alarms only) |
| Two-dial mood engine (energy/warmth, hour baselines, appraise, grudge, restart fade, diminishing praise) | ✅ | `aurora/personality/mood.py`; appraisal has model path + honest lexical fallback |
| Per-person memory & guests | ✅ | `aurora/people.py` (per-subject facts; guest isolation) |
| Idle reflect daemon (episodes, lessons, tidy facts) | ✅ | `aurora/memory/consolidation.py` (continuous, not only daily) |
| Presence/arrival greeting | 🔶 | arrival detection is browser/client-side here; the greeting machinery exists via proactive kinds |
| Barge-in / wake word / push-to-talk / speaker-ID prints | 🔶 | STT/TTS + voiceprint math implemented; mic capture is browser-driven; barge-in/wake-word need real hardware (see LIMITATIONS) |
| Voice server integrations (kokoro-reader, ElevenLabs) | 🔶 | Kokoro HTTP + optional edge-tts; ElevenLabs dropped |
| Tools: files/shell/email/network/desktop/home | ❌ | Aurora's tools: time, reminders, memory (safe-by-default scope, documented) |
| Games, plugins, streaming chat, portrait/VRM, hologram fans, toys, training/LoRA | ❌ | Out of scope for the unified core; portable later via a plugin seam |
| Offline test harness pattern (patched HTTP + stand-ins) | ✅ | replaced by a cleaner design: injectable mock provider + virtual clock |

## From Shikigami Protocol

| Shikigami capability | Aurora | Notes |
|---|---|---|
| FastAPI backend + web dashboard (one page, SSE events) | ✅ | `aurora/server/` |
| Evidence-validated memory extraction with per-source cursor, batch ops (add/supersede), anti-fabrication | ✅ | `aurora/memory/extraction.py` |
| Fact schema (weight, category, versions, archive/merge soft delete, score formula) | ✅ | `aurora/memory/store.py` |
| Hybrid retrieval (BM25+vector RRF) | ✅ | FTS5 + lexical + optional embeddings, RRF-merged |
| Rolling conversation summary | ✅ | running summary with `summary_until_id` cursor |
| Emotion/energy/affinity engines, 8 relationship tiers | ✅ | unified into `mood.py` (dials + affinity) |
| ASE proactive pipeline (urgency, silence gate, cooldown formula, daily caps, no-response backoff, silence protocol, proactive log) | ✅ | `aurora/proactive/scheduler.py`; VLM screenshot step and topic web-research source omitted |
| Topic sources (conversation recall / user life / ai self / random) | ✅ | memory resurfacing / user life / ai self / icebreaker |
| Sleep-window learner, cadence tracker | ✅ | `sleepwindow.py` (cadence simplified into session tracking) |
| Persona evolution (anchor, evidence gating, baseline review, caps, changelog, rollback) | ✅ | `aurora/personality/evolution.py` + corrective retry for dropped headings |
| Durable dated reminders | ✅ | folded into the one reminder system (Aurora adds recurrence + follow-up) |
| Prompt segments with trigger modes | 🔶 | fixed composition order kept; probability/cooldown trigger modes not carried |
| Group chat (multi-persona), lorebooks, SillyTavern import | ❌ | single unified identity is the product; multi-person *users* supported instead |
| Harness (Docker task agent), model gateway | ❌ | heavyweight; Aurora's scope is companion + safe tools |
| Electron shell, setup wizard, Docker | ❌ | one `python run.py` entry instead |

## What Aurora adds that neither upstream had together

Recurrence + follow-up-until-acknowledged + snooze in one reminder store; consolidation that
merges duplicates *and* supersedes contradictions continuously with a rollback-able changelog;
a single journaled initiation path (no competing proactive subsystems); honest no-model
degradation for every feature (reminders fire as plain text, chat states the outage).
