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
  real test (docs/PORTABLE_DEPLOY.md §7 checklist). Barge-in and echo handling are
  **not implemented** (they need a live mic loop). The optional "Hey Aurora" wake-word
  mode IS implemented (browser-local energy VAD + the existing STT endpoint) but is an
  energy threshold, not a neural keyword spotter: expect occasional false triggers in
  noisy rooms, and it needs a secure context (HTTPS/localhost) like all mic access.

### New in this round (reminders/timers/updater) — honest limits
- **Hands-free "Hey Aurora"** — browser energy-VAD + regex over STT text, not a
  dedicated wake-word model. It can false-trigger and will miss soft/noisy
  phrases. Mic audio must not be routed to speakers (feedback with Speak replies
  previously caused stutter transcripts). Push-to-talk remains the reliable path.


- **Chime** is generated in the browser (WebAudio); the browser tab must be open for
  reminders to chime or the bell to update live. Server-side TTS announcements
  (`notifications.voice_announce`) work independently of the tab but need
  `voice.server_speak: true` and a TTS engine.
- **Timer answers** ("repeat" / "five more minutes" / "dismiss") target the most
  recently fired pending timer; with several pending at once, use the per-timer
  buttons (or name the timer — that still goes through the model).
- **Updater** — fully verified live against a LOCAL fake-GitHub release server
  (see TEST_RESULTS.md §0: install → restart → verify → rollback, 18/18, plus the
  Portable-permission reproduction, 28/28). The api.github.com path is the same
  code (`update.api_base`), but **no public Aurora release repository exists yet**,
  so the first real release check on the live service remains to be done once a
  repo + release zip are published. The updater never downgrades the database
  (rollback restores app tree + config only). Release verification is zip-CRC +
  structure + `run.py` presence; no published SHA256SUMS exists yet, so a
  compromised upstream host could serve a valid-zip payload — adding a signed
  digest is the next hardening step if/when a public repo exists.
- **Portable updater reproduction honesty** — the sandbox has no systemd, polkit
  or root, so `scripts/verify_portable_update_e2e.py` reproduces the
  `ProtectSystem=strict` filesystem effect at the permission level (write bits
  removed from the app tree) and executes the helper directly; the privilege
  SEPARATION (root helper + polkit-gated start of one unit) is verified
  structurally (manifest path-escape refusal, fixed script, unit files), not by
  running a real Portable deployment. The privilege boundary now additionally
  rests on root-controlled authorized values (`/etc/aurora-update-authorized.json`)
  and on an explicit extraction allowlist in the rollback path
  (`_safe_extract`: only regular files/directories inside staging, validated
  before any write — verified by the reproducible escape in
  `scripts/verify_tar_vuln.py` pre-fix and its refusal post-fix, plus 9
  dedicated tests). Their uid-0 ownership enforcement and the systemd activation
  conditions are verified structurally and against the published
  systemd.unit(5) semantics —
  not against a live root systemd. The first real run on Portable remains the
  final proof (`docs/PORTABLE_DEPLOY.md §3b`).
- **"in a few hours"-style word-amount durations** still do not parse
  (pre-existing; "in 20 minutes" / "15 minutes" / "tomorrow at 9am" etc. do).
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
