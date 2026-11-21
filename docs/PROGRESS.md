# Progress log (resume point)

## Completed

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
