# Automated tests and their actual results

## 1. Deterministic suite (mock provider + virtual clock)

Command: `python -m pytest tests/ -q --timeout=60` — **232 passed** (20 test files in `tests/`; 3 of the deployment-fix tests are the release-preparation additions: env-file ordering, TLS policy consistency, and an end-to-end self-signed-HTTPS run of verify-voice.sh asserting authenticated 200 + unauthenticated 401).
Coverage: time parsing & recurrence; fact store (dedup/supersede/archive/merge/tombstones/FTS/versions/evidence/export);
extraction (evidence validation, anti-fabrication, op cap, cursor); consolidation (idle gating, summary, dedup merge,
lessons, changelog, LLM review path); mood/personality (grudge/apology, diminishing praise, restart fade, tiers;
evolution cadence, anchor enforcement, heading preservation, rollback, honest no-model skip); reminders (recurrence,
follow-up-until-ack, snooze, missed replay, no-model delivery); proactive (all gates, caps, cooldown, topic dedup,
single-authority journaling); conversation (tool loop, degradation, slash commands, remember/forget, streaming, honest
errors, summary roll, per-person isolation); restart persistence (everything survives, corrupt DB fails loudly);
server API (full CRUD flows, permissions default-deny, API-key redaction); voice/providers (splitting, permission
gating, honest whisper-absent, defensive parsing).

### Portable milestone additions (74 tests, all mock-transport — no LAN needed)

- `test_kayock_tts.py` — speech-adapter compatibility: base-URL normalization; voice-id
  discovery from `GET /v1/models` (OpenAI shape, bare string list, TTL cache, stale-list
  retention + honest error on failure); `POST /v1/audio/speech` request shape (per-call
  voice override, response_format), audio passthrough, 5xx/connect-error → recorded error,
  healthcheck, media types.
- `test_kayock_stt.py` — STT endpoint integration: multipart forwarding of webm/opus
  (filename + content type preserved), model field, `{text}`/alternate JSON parsing,
  service-down and HTTP-error honesty (`available=False`, `last_error`), 404-means-up
  healthcheck, empty-audio short-circuit; local WhisperSTT still accepts the new kwargs.
- `test_voice_selection.py` — voice selection: `/api/voice/voices` discovery, live instance
  + config update on select, unknown voice rejected with the available list, selection
  accepted when discovery is down, **selection survives restart** (real KayockTTS built
  from the saved config), test/tts/stt endpoints incl. media type and webm passthrough,
  voice status reporting.
- `test_model_unavailable.py` — model-unavailable behavior: honest degraded reply marked
  `degraded: model_unreachable` in message meta; reminders still created and delivered
  (plain text) during the outage; post-turn background AI defers with the reason recorded;
  `/api/status` shows unreachable; unloaded-model detection via `/v1/models` listing
  (guard blocks when the configured id is missing, passes when present).
- `test_background_deferral.py` — resource-aware operation: traffic-light busy/free/down/
  disabled detection (GET-only verified), ComputeGuard rules (busy light, unhealthy model,
  foreground grace, kill-switch, TTL-cached model health), proactive tick deferred without
  burning caps (and resuming when free; `force=True` bypasses for the manual button),
  consolidation deferral, deferral recorded for the dashboard.
- `test_reminders_no_inference.py` — reminders and daily operation with **no provider at
  all**: `/remind` scheduling, plain-text delivery, recurring reschedule, missed-reminder
  replay after restart, honest no-model chat replies, memory commands, proactive fallback
  text, crash-free post-turn hooks, lexical-only consolidation.
- `test_service_recovery.py` — restart and recovery: degrade-then-recover chat turns,
  guard TTL back-off and recovery after the model returns, foreground failure marking the
  guard down immediately, deferred proactive firing after recovery, full restart
  preserving voice selection + reminders + history, missed-reminder recovery with the
  model unavailable during replay.
- `test_lan_access.py` — LAN controls: token-gated `/api/*` (401 without, Bearer and
  `X-Aurora-Token` accepted, wrong token rejected), public dashboard page with gated API,
  query token only for the SSE stream, no token → everything open.

### Deployment-fix additions (30 tests — sandbox-safe, Portable parts explicitly skipped)

- `test_deployment_fixes.py`:
  - **shell syntax**: `bash -n` on all five deploy scripts; exec bits present (the zip
    preserves modes).
  - **systemd unit**: `User=aurora` (never root), `Restart=on-failure` + `RestartSec`,
    `NoNewPrivileges`/`ProtectSystem=strict`/`PrivateTmp` retained,
    `ReadWritePaths=/var/lib/aurora /etc/aurora` with the app tree **not** writable,
    EnvironmentFile + bytecode-skip env.
  - **installer permissions**: `/etc/aurora` chowned to the service account, `chmod 750`
    dirs, `chmod 600 config.yaml`, `aurora.env` `root:aurora` 640, world-access find
    check, `runuser`-based per-path verification before start with abort-on-failure,
    startup + persistence smoke under the service account; **no** world-writable chmod
    anywhere in the installer; `--tls` wiring to `make-cert.sh`.
  - **verify-voice.sh**: `AUTH_ARGS` array used (no `$(auth)` word-splitting), token value
    provably never printed (line-by-line audit), 401 checked via captured `%{http_code}`,
    `-k` only for HTTPS, embedded Python snippets extracted and `ast`-compiled (catches
    the old Python<3.12 f-string SyntaxError), read-only probes + the one-shot test
    speech/transcription retained, no mutating verbs in code lines.
  - **personalization**: `user_name: "Eric"` in the Portable template; persona seed has no
    "Eric" (Aurora's identity untouched); evolution cadence unchanged.
  - **TLS**: `run.tls_options` — both paths → uvicorn kwargs, missing/partial/empty →
    no TLS, defaults off. (A generated certificate was additionally proven to serve
    Aurora over HTTPS in a live sandbox run — outside pytest.)
  - **permission model round-trip**: the installer's exact modes applied to a temp tree,
    then `Config.load → set(voice.voice_id) → save → reload` succeeds and the file
    **stays 600** (token protected) — i.e. persistence works without loosening
    permissions; no world-accessible bits anywhere.
  - **voice + auth**: `/api/voice/voices` and `/api/voice/status` gated (401) and fully
    usable with the token — voice verification never requires disabling auth.
  - `verify-permissions.sh` content matches the model (600/750 checks, world-access find,
    `runuser` smoke, honest SKIP for non-root).

**Beyond pytest (sandbox, reported for transparency):** `verify-voice.sh` run against a
mock Kayock/Aurora ecosystem (mock TTS/STT + token-gated Aurora): valid token → all
checks PASS incl. HTTP-401 rejection, exit 0; wrong token → authenticated requests
rejected, exit 1; token value never appears in output. `verify-permissions.sh` run
against a simulated installer layout: all mode/ownership checks PASS, service-account
smoke honestly SKIPped (needs root/Portable). A `make-cert.sh` certificate (SANs:
kayock-Ubuntu-Portable, localhost, 10.0.0.45, 127.0.0.1) served Aurora over HTTPS with
`curl -k`. **On-site only**: real `aurora`-user systemd startup (`verify-permissions.sh
--smoke`), voice hardware, live Kayock binaries, LAN reachability, Traffic Light protocol.

## 2. Real-model integration — PASSED end-to-end

`scripts/aurora_integration.py` ran Aurora against a **live GLM** through an OpenAI-compatible localhost proxy — the
*same* `openai_compat` provider used for LM Studio / llama.cpp. Final run (raw output: `integration_results.json`,
`integration_transcript.md`):

| Step | Result | Evidence |
|---|---|---|
| Real chat turns (persona + mood + memory in prompt) | ✅ | two in-persona replies naming Sam, Riverside Park, March 15 |
| Memory extraction (real LLM ops, evidence-validated) | ✅ | 2 facts added with evidence; 1 duplicate refused by Jaccard dedup live |
| Recall of stored facts in a later reply | ✅ | reply referenced running/marathon memories |
| Reminder delivered through the model | ✅ | "Time to drink some water before your next run!…" — scheduler fired, model phrased |
| Persona evolution (evidence-gated) | ✅ | v3; honest "no changes — insufficient evidence" instead of forcing a change |
| Idle consolidation | ✅ | merged=4, superseded=1 on real facts |
| Mood/affinity | ✅ | warmth 1.0, energy 0.59, affinity 54 ("friendly stranger") |
| Restart persistence | ✅ | facts 10→4 active, history=7, persona_v=3 intact after close+rebuild |

Earlier runs found and fixed real defects: false-positive "remember…" regex, over-strict evidence-quote matching,
missing evolution heading-retry, reminder instructions not reaching the model, no provider retry/backoff. One run
demonstrated honest degradation when the upstream went down: visible "couldn't reach my language model" message,
plain-text "⏰" reminder fallback, persistence intact.
