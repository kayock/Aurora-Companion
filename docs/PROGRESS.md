# Progress log (resume point)

## Completed

0c. **Privileged rollback extraction correction (2026-11-21).** Confirmed and
reproduced the unsafe `extractall()` in the rollback path (traversal member
escaped outside the authorized data dir, absolute member written at its
absolute location, malicious tree replaced the app — helper exit 0). Replaced
with `_safe_extract`: explicit allowlist validated before any write (no
absolute/`..` members, strictly-inside-staging resolution, symlinks/hardlinks/
devices/FIFOs rejected, normalized 0755/0644 modes), clean refusal (exit 2,
staging removed) for malformed/suspicious archives, live app untouched on
failure. 9 new regression tests → suite 287; portable E2E 29/29 runs the
rollback through the new extractor; `scripts/verify_tar_vuln.py` documents
pre-fix escape / post-fix refusal.

0b. **Deployment corrections + security verification (2026-11-21).** All three
review findings confirmed and fixed: (1) root helper now pins app_dir/data_dir/
unit/config_path to root-controlled authorized values with full path
canonicalization — a writable manifest can no longer redirect privileged
operations, restart foreign units or overwrite foreign config files (13 new
security tests in `tests/test_update_helper.py`); (2) installer ownership fixed
(`install -m 0755 -o root -g root`, previously an invalid positional that aborted
the installer under `set -e`) and install.sh writes the authorization file;
(3) activation conditions fixed per systemd.unit(5) triggering-condition
semantics (rollback-only requests previously never activated the helper unit).
Suite 265 → 278; E2Es: portable 29/29, updater 18/18, reminders 19/19.

0a. **Updater hardening round (2026-11-21).** Both review findings confirmed and
fixed: (1) on Portable (`ProtectSystem=strict` + `NoNewPrivileges`) the updater
could not touch the app tree and could not elevate — new assisted mode stages in
the data dir and hands a validated manifest to `aurora-update-helper` (root-only)
via `aurora-update.service` + a polkit rule authorizing only that unit; (2) rollback
recreated the tree without the excluded `venv/`/`.git/` — local state is now
carried aside and moved back. Bonus fixes found by the new
`scripts/verify_portable_update_e2e.py` (28/28: DAC-level reproduction, honest
about no systemd): helper carry dir, manifest consumption, backup mode
normalization, event-loop-independent `restart_soon`. Suite 261 → 265.

0. **Upgrade milestone (2026-11-21) — reminders/timers/notifications, hands-free,
   one-button updater, update awareness.** Repository audit first: HEAD at the
   release-prep commit, 103 mode-only (644→755) working-tree changes and an empty
   `tools/` dir — **no content changes existed to preserve or conflict with**; modes
   normalized in the final commit. Then: deterministic NL reminder/timer fast-paths
   (model-free, bare durations included), timer `kind` + repeat/snooze/dismiss flow
   ("Your timer is up. Would you like me to repeat it?", speak-once, multi-timer,
   restart-persistent, never auto-restart), defensive execution of tool-call JSON
   leaked as text, notification bell + WebAudio chime + optional voice announce,
   opt-in "Hey Aurora" hands-free over the existing STT, `aurora/updater.py` with
   backup/verify/swap/carry/restart/verify/rollback (+ Settings UI + `/api/update/*`),
   software-state prompt block + update history, junk-reminder rejection at the store,
   `Database.backup_to`, additive schema migration. Tests 232 → 261; two live E2E
   scripts added (`scripts/verify_reminders_e2e.py`, `scripts/verify_update_e2e.py`
   — updater cycle 18/18 PASS); updater restart bugs found and fixed by the E2E.
1. **Inspection** — both repos cloned to `/home/z/my-project/repos/` (not modified); deep
   inventories produced (architecture, storage schemas, algorithms, dependencies, reuse
   assessments for both).
2. **Architecture decision** — new unified codebase (`aurora-companion/`); rationale in
   ARCHITECTURE.md; both-upstream attribution in NOTICE.md.
3. **Implementation** — config/storage/clock/util → providers (openai_compat, mock,
   embeddings) → timeparse → memory (store/retrieval/extraction/consolidation/changelog)
   → personality (persona/evolution/mood) → reminders (store/scheduler) → proactive
   (scheduler/gates/sleepwindow/topics) → conversation (manager/prompt/tools) →
   people/permissions/senses → voice (tts/stt/service) → server (REST+SSE) → single-file
   dashboard → `run.py`.
4. **Tests** — 124 passing (`python -m pytest tests/ -q`), incl. full restart persistence
   and honest no-model paths.
5. **Real-model integration** — runs 1 & 2 against a live GLM via an OpenAI-compatible
   proxy; findings fixed (remember-regex false positive, evidence-quote tolerance,
   evolution heading retry, reminder-instruction visibility, provider retry/backoff,
   graceful degradation); raw outputs in docs/integration_results.json +
   docs/integration_transcript.md.
6. **Checkpoint** — committed in the repo's git history.
7. **Portable milestone** (this release) — Ubuntu Portable (`kayock-Ubuntu-Portable`,
   10.0.0.45) selected as the permanent host. Implemented inside Aurora only:
   - Kayock voice integration: `KayockTTS` (OpenAI-compatible speech adapter, live
     voice-id discovery from `/v1/models`, mp3/wav) + `KayockSTT` (Faster-Whisper service,
     webm/opus passthrough); dashboard voice picker + test button; mic push-to-talk
     (webm/opus → STT); all existing services used read-only, never configured.
   - Resource-aware operation: `ComputeGuard` + read-only traffic-light prober
     (disabled until protocol verified on-site); background AI (mood, extraction,
     evolution, consolidation, summary, proactive) defers when the model is
     busy/unloaded/unavailable or compute is needed elsewhere; honest degraded chat with
     `degraded` message meta; reminders/memory/dashboard fully model-independent.
   - Ubuntu deployment: `deploy/aurora-companion.service`, `install.sh` (port inspection,
     venv, systemd registration, generated auth token), `config.portable.yaml`,
     `aurora.env.example`, `backup.sh` (SQLite online backup + retention),
     `verify-voice.sh` (post-install connection checklist incl. traffic-light protocol
     dump).
   - LAN access: optional token auth on `/api/*` (Bearer / X-Aurora-Token / SSE ?token=),
     public dashboard page, dashboard token flow; mic/camera still double-gated.
   - Tests: 125 → **199 passing** (7 new suites, mock transport — no LAN needed);
     docs: PORTABLE_DEPLOY.md + updated INSTALL/ARCHITECTURE/LIMITATIONS/TEST_RESULTS.
   - Not verified in the sandbox (honest): real audio hardware, live Kayock binaries,
     LAN reachability, the Traffic Light's actual protocol — the on-site checklist
     (PORTABLE_DEPLOY.md §7) covers exactly those.
8. **Deployment-fix update** (this release, targeted — no architecture changes):
   - Installer permissions corrected: `/etc/aurora` now owned by the service account
     (750), `config.yaml` 600 `aurora:aurora` (readable **and** persistable for voice
     selection, auth token hidden from other users), `aurora.env` root:aurora 640; the
     installer verifies every path **as the service account** (`runuser`) and smoke-tests
     startup + config persistence before starting the unit; hardening kept and tightened
     (`ProtectSystem=strict`, app tree read-only, only data+config writable).
   - `verify-voice.sh`: auth header now a proper Bash array (`AUTH_ARGS`), correct HTTP-401
     capture for the reject check, embedded Python snippets rewritten for Python 3.8–3.11
     compatibility (the old f-string `\` escapes were a SyntaxError), token never printed,
     HTTPS `--insecure` only for the local self-signed cert. Verified end-to-end against a
     mock Kayock/Aurora ecosystem in the sandbox (valid token PASS / wrong token rejected /
     no leakage).
   - `deploy/verify-permissions.sh` added: layout + mode/ownership checks, service-account
     smoke (root/Portable only, honest SKIP otherwise); `deploy/make-cert.sh` added.
   - Secure LAN push-to-talk: optional TLS on Aurora's own port (`server.tls_cert/key` +
     `run.py tls_options`, proven serving HTTPS in the sandbox), dashboard disables the 🎤
     with an explanatory hint in insecure contexts; push-to-talk design, permissions and
     Kayock integration unchanged; no new listening or wake-word behavior.
   - `config.portable.yaml`: `user_name: "Eric"` (Aurora's own identity/personality
     untouched).
   - Tests: 199 → **229 passing** (30 deployment-fix regressions incl. bash syntax, unit
     content, permission-model round-trip, TLS options, voice endpoints under auth).
9. **Release-preparation update** (this release, documentation + final deployment fixes):
   - Final permission inconsistencies corrected: `aurora.env` is now installed **before**
     the permission block (explicit `0640 root:aurora` on clean install and reinstall,
     verified pre-start); TLS policy made internally consistent — cert AND key `0640
     root:aurora` in a `0750 root:aurora` tls dir (the cert was `0644`, which conflicted
     with the verifier's no-world-access rule); `verify-permissions.sh` gained explicit
     TLS checks; `verify-voice.sh`'s unauthenticated 401 probe now honors the local
     self-signed HTTPS handling.
   - New functional regression: a pytest runs `verify-voice.sh` end-to-end against a real
     self-signed HTTPS endpoint (certificate generated by `make-cert.sh`) and asserts
     authenticated 200 + unauthenticated 401; tests 229 → **232 passing**.
   - GitHub release package: rewritten README (genuine screenshots, demo walkthrough,
     honest limitations, attribution), 8 authentic dashboard captures from the fictional
     demo database under `docs/screenshots/`, conceptual logo + social-preview artwork
     under `docs/assets/`, `docs/GITHUB_RELEASE.md`, `CHANGELOG.md`, `CONTRIBUTING.md`,
     extended `.gitignore`, and `scripts/make_demo_db.py` (mock-provider demo data).
   - Mock provider's default reply is now a clearly labeled offline notice.

## Next steps (in priority order)

0. Run `deploy/install.sh` + `deploy/verify-voice.sh` + `deploy/verify-permissions.sh
   --smoke` on Portable and complete the §7 checklist; confirm the Traffic Light's JSON
   shape and enable `resources.traffic_light_url`.
1. Re-run the uninterrupted real-model integration (any OpenAI-compatible endpoint) and
   update TEST_RESULTS.md.
2. Voice hardware pass: browser mic → `/api/voice/stt` → turn → `/api/voice/tts`; then
   barge-in/echo handling.
3. Wire an embeddings endpoint and validate RRF recall against real vectors.
4. Expand the tool registry (files/web search) behind the existing permission registry.
5. Multi-party sessions: speaker attribution per message in one shared session.
6. Dashboard polish: per-message tool events, memory diff view for consolidation runs.

## Environment notes

Both upstreams remain unmodified at `/home/z/my-project/repos/`. The OpenAI-compatible
proxy used for the real-model runs lives in `/home/z/my-project/scripts/zai-proxy/`
(sandbox tool, not part of the deliverable).
