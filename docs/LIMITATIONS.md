# Known limitations and remaining work

Honest status of every desired capability.

## Works (verified by the automated suite; see TEST_RESULTS.md)

Unified conversation/manager, persistent memory with evidence + supersession + tombstones +
changelog, evidence-validated extraction, idle consolidation (summaries, dedup, contradiction
supersede via model ops, lessons), persona evolution with anchor/rollback, mood/affinity,
proactive scheduler with all gates and dedup, general-purpose reminders (recurring,
follow-up, snooze, missed recovery, model-independent delivery), restart persistence of all
state, permissions, per-person memory, dashboard over the same APIs.

## Partially working

- **Voice input/output** — implemented end-to-end in code and covered by the automated
  suite against the real HTTP dialects of the host's voice services (Kayock speech adapter
  TTS with live voice discovery, Faster-Whisper STT forwarding webm/opus, plus local
  Kokoro/whisper alternatives). **Never exercised against real audio hardware or the live
  Kayock binaries in the build environment** — the first hardware pass on Portable is the
  real test (docs/PORTABLE_DEPLOY.md §7 checklist). Barge-in, echo handling and wake words
  are **not implemented** (they need a live mic loop).
- **GPU Traffic Light coordination** — a generic read-only prober ships (GET
  /health|/status|/ + configurable busy markers), disabled by default; its real protocol
  must be verified on Portable before enabling. Fail-open by design (a broken light never
  blocks Aurora).
- **Semantic (vector) recall** — implemented with an OpenAI-compatible embeddings client +
  SQLite cache + RRF fusion, but only exercised with the mock in tests; enable
  `embeddings.enabled` to use it.
- **Speaker identification** — voiceprint enrollment/matching is implemented and unit-tested
  (pure cosine math); there is no automatic speaker embedding pipeline (no SpeechBrain
  integration) — prints must be provided by a client.
- **Screen/camera awareness** — permission-gated, client-supplied frames via
  `/api/vision/screen` for vision-capable models; untested against a real VLM; frames are
  never persisted.
- **Multi-person conversations** — per-person memory separation and guests are tested;
  simultaneous multi-party turn attribution in one session is configured but basic
  (no speaker diarization).
- **Real-model end-to-end run** — see TEST_RESULTS.md run 1/2: pipeline verified in parts;
  the uninterrupted full loop was blocked by upstream rate limits in this environment.

## Not implemented (documented, not hidden)

- Tool scope: time/reminders/memory only — no files, shell, email, web search, home
  automation, MCP, games, streaming chat, plugins (both upstreams' toolsets were much
  larger; Aurora chose a safe core).
- Persona-evolution *trigger modes* from Shikigami's segment framework (probability,
  cooldown, keyword) — Aurora uses one cadence.
- Lorebooks, SillyTavern import, group chat, Docker packaging, Electron shell.
- Wake-word/barge-in voice loop; push-to-talk hotkeys.
- Traffic-light write operations will never exist (Aurora is read-only toward other
  services' resources); only the marker configuration needs on-site confirmation.
- Day-summary cadence is consolidation-driven; there is no separate 00:05 daily job.

## Known rough edges

- The learned sleep window needs ≥3 days of message data before it tightens quiet hours.
- Extraction quality depends on the background model; bad JSON batches are skipped, not
  repaired (check the Memory → changelog panel).
- The dashboard is a single-file vanilla-JS app: functional, not pixel-polished.
