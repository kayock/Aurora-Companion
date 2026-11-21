# Automated tests and their actual results

## 0c. Privileged rollback extraction correction (2026-11-21)

**Vulnerability CONFIRMED and reproduced against the real helper** (not just by
inspection): with `scripts/verify_tar_vuln.py`, a rollback backup archive
(service-user-writable) containing a 4-level traversal member escaped outside
the authorized data directory, an absolute member was written at its absolute
location, and the malicious tree replaced the live application — helper exit 0,
Python 3.12.16 with `extractall()` defaults. On a real Portable deployment the
helper runs as root, so this is a service→root privilege escalation path.

**Fix:** `deploy/aurora-update-helper::_safe_extract` — explicit allowlist,
validated BEFORE any write: no absolute paths, no `..` components, member paths
must resolve strictly inside staging, symlinks/hardlinks/devices/FIFOs rejected,
only regular files and directories extracted member-by-member with normalized
modes (0755/0644) — independent of Python-version-dependent tarfile defaults.
Malformed/truncated archives are refused cleanly (exit 2, staging removed,
traceback-free) and the live application is only touched after successful
extraction and layout verification.

**Regression tests:** command `python -m pytest tests/ -q` — **287 passed**
(278 + 9 new): valid archive rollback with venv/config/database preservation;
traversal member; absolute member; symlink member; hardlink member; FIFO and
character-device members; truncated archive (clean refusal, staging removed);
traversal attempting to overwrite the live tree; no-write-before-validation.
Each negative test also asserts the live application is untouched.

**Reproduction script:** `scripts/verify_tar_vuln.py` — post-fix run: helper
exit 2, `REFUSED: backup archive contains a traversal member`, no escape files
created, live app untouched.

**Live end-to-end:** portable reproduction **29/29 PASS** (the rollback path in
it runs through the new extractor end-to-end: staging → validation →
extraction → carry/restore → manifest consumption), updater **18/18 PASS**,
reminders/voice/notifications **19/19 PASS**. Simulated/structural scope
unchanged: no systemd/polkit/root in this sandbox; the first real Portable run
remains the final proof (docs/PORTABLE_DEPLOY.md §3b).

## 0b. Deployment corrections + security verification (2026-11-21)

Command: `python -m pytest tests/ -q` — **278 passed** (265 + 13 new helper
security/scenario tests). All three review findings were CONFIRMED in code and
fixed:

1. **Helper privilege boundary** — the manifest (service-user-writable) could set
   `app_dir` to any directory (incl. `..` traversal to `/etc`), `unit` to any
   service (root restarts it), and the rollback `config_path` to any root file
   (backup content copied over it). The helper now pins app_dir/data_dir/unit/
   config_path to root-controlled authorized values (`/etc/aurora-update-
   authorized.json`, root-owned 0644, written by install.sh; test-only env
   override for the reproduction E2E), canonicalizes every path (symlinks
   resolved) before comparison, and refuses everything else — consistently for
   install/rollback/validate. Negative tests: foreign app_dir, `..` traversal,
   symlink escapes (tree + staging), foreign unit, foreign config_path, staging
   outside the authorized data dir, unwritable authorization file, invalid keep
   list, both-manifests state, none-pending inertness.
2. **Installer ownership syntax** — `install -m 0755 root:root …` parses
   `root:root` as a SOURCE (GNU install takes `-o/-g`); with `set -euo pipefail`
   the installer aborted at that line. Demonstrated in an isolated temp
   environment (old command errors, corrected command installs mode 0755 with
   explicit ownership); fixed to `install -m 0755 -o root -g root`, and
   install.sh now also writes the root-owned authorization file for non-default
   install paths.
3. **Rollback activation** — verified against systemd.unit(5): conditions are
   ANDed; `|` marks a triggering condition; triggering conditions OR among
   themselves but are ANDed with every regular condition — so the previous unit
   started only when BOTH manifests existed and a rollback-only request could
   never activate. Both conditions are now triggering; the four scenarios are
   covered by helper-level tests (the sandbox has no systemd to run the unit
   itself — stated honestly, structural + semantics-doc verification).

The helper's four activation scenarios and refusal paths run through
`tests/test_update_helper.py` as real subprocess invocations of
`deploy/aurora-update-helper` (the same entry point systemd calls).

**Live end-to-end — `scripts/verify_portable_update_e2e.py`: 29/29 PASS** (28
previous checks + the new negative check: a manifest claiming a foreign app_dir
is refused by the helper). Same honest scope as §0a: permission-level
reproduction of the read-only restriction, no systemd/polkit in the sandbox.
**Live end-to-end — `scripts/verify_update_e2e.py`: 18/18 PASS** (direct mode
unchanged). **Live end-to-end — `scripts/verify_reminders_e2e.py`: 19/19 PASS**
(reminders/timers/notifications/hands-free unchanged).

## 0a. Updater hardening / Portable deployment round (2026-11-21)

Command: `python -m pytest tests/ -q` — **265 passed**. New in this round: 4 updater
unit tests — rollback preserves `venv/`, `.git/`, `config.yaml` and `aurora.env`
(the backup archive excludes them by design; they are carried aside and moved back),
rollback after an INTERRUPTED update refuses honestly and changes nothing, apply()
on a read-only app tree stages the release inside the data directory with a
validated manifest and touches nothing in the tree, and the root helper's manifest
validation (including refusal of path escapes outside the data dir). Also fixed by
this round's runs: `restart_soon()` depended on a managed asyncio event loop
(`get_event_loop().create_task` broke inside pytest-asyncio) — replaced by a daemon
thread; backups now store normalized modes so a backup captured from a read-only
tree stays extractable.

**Live end-to-end — `scripts/verify_portable_update_e2e.py`: 28/28 PASS.** Reproduces
the Portable restrictions at the permission level (app tree + parent made
owner-unwritable, mirroring the EFFECT of `ProtectSystem=strict`; the sandbox has no
systemd, so this is a faithful filesystem-effect reproduction, NOT a Portable
deployment test): read-only `apply` → staged manifest, app tree untouched → helper
manifest validated → helper install with venv/.git/config.yaml/aurora.env carried
across → restart → new version + "I'm back!" + memories/reminders intact → read-only
rollback staging → helper rollback with the venv still usable → restart → previous
version + data intact → history records the rollback. **The run found and fixed
three real defects**: the helper never created its carry directory; manifests were
not consumed after applying (a later rollback collided with the old install
manifest); and backups captured from a locked tree restored unwritable directories.

**Live end-to-end — `scripts/verify_update_e2e.py`: 18/18 PASS** (direct mode,
unchanged semantics after the hardening — same cycle as §0: check → install →
execv restart → verify → rollback → corrupt-zip refusal).

**GitHub release readiness audit:** asset name `Aurora-Companion.zip` matches the
updater default and `package_aurora.py`; `v`-prefixed tags handled; zip layout
(single `aurora-companion/` root + `run.py`) matches the updater's structure check;
packaging excludes config/data (the updater carries local config across). Two
alignment gaps fixed in the docs: `Aurora-Companion.zip` is REQUIRED on a release
(the updater cannot install a tag-only release), and `update.repo` must be set in
`/etc/aurora/config.yaml` on Portable for checks to run (default empty = disabled).

## 0. Reminders/timers/notifications + updater upgrade (2026-11-21)

Command: `python -m pytest tests/ -q` — **261 passed** (23 test files). New in this
round: 17 reminder/timer/notification tests (NL "remind me …"/"set a timer for …"
model-free creation incl. bare durations, timer completion prompt with buttons,
repeat restoring the original duration — recorded exactly on the row at creation,
snooze/bring-back and re-fire, dismiss, spoken one-shot answers ("repeat"/"five more
minutes"/"dismiss"), several independent timers, pending decisions surviving a
restart, voice announcement exactly once (with and without a model), junk-text
rejection ("hi"/"null"/blank) at the API, chat fast-path and store,
confirm-only-after-execution, `kind=timer` via the model tool, legacy-DB migration
without data loss), 9 updater unit tests (version ordering, release checking against
a stubbed GitHub API incl. network-error honesty, **mid-stream interrupted download**
and corrupt-zip refusal leaving the tree untouched, the swap carrying
venv/.git/config.yaml across, verify-after-restart for both outcomes, database
online snapshot) and defensive tool-call JSON recovery in the conversation loop.

**Live end-to-end (real server, real waits) — `scripts/verify_reminders_e2e.py`: 19/19 PASS.**
chat reminder → stored → confirmed only after execution → bare-duration dashboard
form entry → junk rejected over HTTP → 90-second timer → SSE `reminder_due` +
`timer_due` → bell payload with the pending decision → repeat restores the original
90 s → re-fire asks again → snooze 60 s → re-fire keeps duration exactly 90 →
dismiss → restart persistence of active reminders. (A first run found two real
defects — timer duration drifting with wall-clock skew, and a test-side SSE window
mismatch — the duration defect was fixed by recording the intended length on the
row at creation; the rerun passed 19/19.)

**Live end-to-end — `scripts/verify_update_e2e.py`: 18/18 PASS.** Real server +
local fake-GitHub: version check (0.9.0 → 1.0.0 available, notes carried) →
install (backup tar + config copy + online SQLite snapshot written; venv/.git/
config.yaml carried across the swap) → restart via `os.execv` → new version serves
→ "I'm back!" announcement posted → history records `ok` → facts, reminders and
persona survive → rollback restores 0.9.0 with memories intact → history records
`rolled_back` → corrupt zip refused with the tree untouched. The two updater bugs
this run caught and fixed (restart after the tree swap: relative `sys.argv[0]`
plus a deleted cwd; `config.yaml` not carried across the swap) are exactly the
class of failure unit tests could not reach.

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
