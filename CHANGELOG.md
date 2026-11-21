# Changelog

All notable changes to Aurora Companion. The project follows semver-ish versioning
(see docs/GITHUB_RELEASE.md §6). Dates are UTC.

## [1.0.4] — 2026-11-21

### Fixed
- **Unauthorized reminders:** a well-formed `set_reminder` tool call is no longer
  enough to create a task. The current user message must clearly ask to schedule
  (remind / timer / remember-to / don't-forget-to). Greetings and topic chat that
  caused four "water the basil" reminders on 1.0.3 are rejected with no side
  effects. Proactive turns cannot authorize scheduling. Quoted "remind me" does
  not authorize. Deterministic NL / slash / dashboard paths remain unchanged.
- **remember_fact** tool similarly requires an explicit remember request so the
  model cannot invent durable owner facts from ordinary chat.
- Pytest refuses to open `/var/lib/aurora`, so the automated suite cannot
  contaminate the live Portable database.

### Notes
- Confirmed development/test contamination in the live DB is inventoried for
  owner-approved cleanup; 1.0.4 does not delete personal records automatically.

## [1.0.3] — 2026-11-21

### Fixed
- Tool-call JSON leaked into chat when `llama3.2:3b` emitted plain-text
  `{"name":…,"parameters":…}` (often with a missing colon). Aurora now repairs
  common malformations, executes valid embedded tool calls, suppresses their
  stream deltas (`stream_reset`), and never persists tool JSON as a reply.
- Conversation stickiness: memory retrieval uses the current turn only; leaked
  tool-JSON assistant rows are filtered from the model window; prompt prioritizes
  the latest user request and exact short instructions.
- Hands-free: utterances are queued while STT is busy (no longer drop the
  command after "Hey Aurora"); softer wake matching for Whisper misspellings;
  listening indicator shows why an utterance was ignored.
- Latency: skip FTS/embedding retrieval for directive/greeting turns.

## [1.0.2] — 2026-11-21

### Fixed
- Reminders dashboard listed every status, so **Done** / **Cancel** looked broken
  even though the database updated. Active / History / All views now filter
  correctly; completed and cancelled rows leave Active immediately.
- Permanent **Delete**, multi-select **Delete selected**, and **Clear history**
  (delivered / done / cancelled / missed only) are wired through the API and UI.
- Phantom reminders from greetings, break nudges, truncated check-ins, and model
  self-intros are rejected at the store (every create path) and the set_reminder
  tool returns an error instead of storing them.
- System prompts no longer inject relative countdown phrasing for upcoming
  reminders, and guardrails tell the model not to schedule or recite reminders
  unless the user asks (or a delivery turn is in progress).
- Proactive work-break / morning frames instruct the model to speak only — never
  invent scheduled tasks on initiation turns.

### Notes
- Next GitHub release candidate after local verification on Portable. Do not
  push until the owner requests publication.

## [1.0.1] — 2026-11-21

### Fixed
- Dashboard chat: streaming deltas no longer rewrite whole message nodes, which
  previously concatenated the `"chat"` source label into reply text and could
  show duplicate assistant bubbles (POST + SSE). Content lives in `.msg-body`;
  final messages are upserted by id.
- Hands-free ("Hey Aurora"): mic graph no longer routes to speakers (feedback
  loop with Speak replies); listening pauses during TTS playback; overlapping
  STT submissions are gated; ambient non-wake speech is ignored while idle.
  Wake detection remains energy-VAD + regex — not a dedicated wake-word model.
- Kayock voice discovery prefers `GET /v1/voices` (Portable adapter) with
  `/v1/models` fallback.
- Ollama multi-round tool calls use OpenAI-shaped `tool_calls` / `role=tool`
  messages; tool failures rebuild a clean prompt before retrying without tools.

### Notes
- GitHub tag `v1.0.0` shipped application code that still reported `__version__`
  `0.2.0`. This release aligns the in-app version with the 1.0.x line.

## [Unreleased]

### Fixed (privileged rollback extraction, 2026-11-21)
- **Unsafe TAR extraction (confirmed + fixed):** the rollback path used a plain
  `tarfile.extractall()`, which on Python 3.12/3.13 extracts fully trusted by
  default. The backup archive lives in the service-user-writable data directory
  and the helper runs as root — reproduced against the real helper: a
  traversal member escaped outside the authorized data directory, an absolute
  member was written at its absolute location, and a malicious tree replaced
  the live application (scripts/verify_tar_vuln.py). The helper now extracts
  with an explicit allowlist (`_safe_extract`): every member is validated
  BEFORE anything is written — absolute paths and any `..` component are
  rejected, every member's final path must resolve strictly inside staging,
  symlinks/hardlinks/devices/FIFOs are rejected, and only regular files and
  directories are extracted member-by-member with normalized modes (0755
  dirs / 0644 files), version-independently. A malformed, truncated or
  suspicious archive is refused cleanly (exit 2, staging removed) before the
  live application is touched.

### Added (privileged rollback extraction, 2026-11-21)
- 9 safe-extraction regression tests (valid archive + venv/config/database
  preservation, traversal, absolute member, symlink, hardlink, FIFO/device,
  malformed archive with clean refusal, overwrite attempt against the live
  tree, no-write-before-validation). Suite now **287 passing**; the portable
  reproduction E2E exercises the full rollback through the new extractor.
- `scripts/verify_tar_vuln.py` — isolated reproduction that demonstrates the
  escape against the pre-fix helper and the refusal against the fixed one.

### Fixed (deployment corrections + security verification, 2026-11-21)
- **Root helper privilege boundary (confirmed + fixed):** the manifest is written
  by the service user, and the helper previously trusted the manifest's `app_dir`,
  `unit` and rollback `config_path` — a writable manifest could redirect privileged
  operations to another tree (incl. via `..` traversal), restart another systemd
  unit, or copy backup content over an arbitrary root file. The helper now acts on
  ROOT-CONTROLLED authorized values only (`/etc/aurora-update-authorized.json`,
  root-owned 0644, written by install.sh for non-default installs; built-in
  defaults otherwise). Every manifest path is canonicalized (symlinks resolved)
  BEFORE comparison; foreign destinations, traversal, unexpected unit names and
  non-subset keep-lists are refused — identically for install, rollback and
  validate. A test-only env override exists; the systemd unit sets no environment.
- **Installer ownership syntax (confirmed + fixed):** `install -m 0755 root:root …`
  parsed `root:root` as an extra SOURCE and aborted the whole installer under
  `set -e` (GNU install takes ownership via `-o/-g`). Now
  `install -m 0755 -o root -g root …`; syntax verified in an isolated environment.
- **Rollback activation conditions (confirmed + fixed):** systemd conditions are
  ANDed; the pipe symbol marks a TRIGGERING condition, and triggering conditions
  are ORed among themselves but ANDed with every regular condition (systemd.unit(5)).
  The previous mix (one regular + one triggering) started the unit only when BOTH
  manifests existed — a rollback-only request could never activate. Both conditions
  are now triggering; install-only / rollback-only activate, both-pending is
  refused by the helper, none-pending is an inert exit-0.
- Helper `apply` now refuses suspicious states cleanly (exit 65 with a journal
  line) instead of an unhandled traceback; `_move_back` never leaves a
  half-restored tree when a restored backup contains its own copy of a carried
  directory.

### Added (deployment corrections + security verification, 2026-11-21)
- `tests/test_update_helper.py` — 13 security and activation-scenario tests run
  against the helper as a subprocess: foreign app_dir refused, `..` traversal
  refused, symlink escapes refused (app_dir and staging), foreign unit refused,
  foreign rollback config_path refused, staging outside the authorized data dir
  refused, unwritable authorization file refused, invalid manifests cannot trigger
  changes, and the four activation scenarios (install-only, rollback-only,
  both-pending refused, none inert). Suite now **278 passing**.
- The portable reproduction E2E now pins the helper's authority explicitly and
  adds a negative check (a manifest claiming a foreign app_dir is refused).

### Added
- **Reliable reminders & notifications**: natural-language requests ("Remind me to
  check the servers in 15 minutes", bare durations like "15 minutes" too) execute
  model-free and deterministically; a dashboard notification bell shows active
  reminders and timer decisions (Done / Snooze / Dismiss controls preserved and
  extended); a gentle two-tone browser chime (WebAudio, no audio files) fires on
  delivery; optional voice announcements of reminders and timers
  (`notifications.voice_announce` + `voice.server_speak`); models that answer with
  tool-call JSON as plain text have that call executed instead of leaking JSON
  into the chat — a reminder request always runs, and Aurora only confirms after
  it is actually stored.
- **Timer interaction flow**: timers (`kind=timer`, created via "set a timer for …"
  in chat, the dashboard form or the model's tool) ask exactly once — "⏱️ Your timer
  is up. Would you like me to repeat it?" — with Repeat (original duration restored),
  Snooze (bring-back; original duration kept for a later repeat) and Dismiss as
  buttons and as spoken answers ("repeat", "five more minutes", "dismiss"); several
  timers stay independent; unanswered decisions persist across restarts, are shown in
  the bell, are never re-prompted and never auto-restart.
- **Optional hands-free mode**: opt-in "Hey Aurora" listening in the dashboard —
  browser-local energy VAD (AudioWorklet) captures an utterance only after speech is
  detected and sends it to the EXISTING local STT endpoint (no new dependencies);
  wake phrase + command ("Hey Aurora, remind me to…") or wake phrase then a follow-up
  utterance; visible listening indicator; push-to-speak unchanged.
- **One-button software update** (`aurora/updater.py` + Settings → Software update):
  checks the configured repo's latest stable release, shows version + release notes,
  asks permission, backs up the app tree + config + an online SQLite snapshot, then
  downloads (interrupt-safe `.part` + zip CRC verification) and swaps the tree,
  carrying `venv/`, `.git/`, `config.yaml` and `aurora.env` across; restart via
  passwordless `sudo systemctl` on systemd deployments or in-place `os.execv`
  elsewhere; on startup the new version verifies itself, records the outcome in the
  update history and posts "I'm back! The update worked…"; rollback restores the
  previous tree (never an older database). Corrupt or interrupted downloads abort
  before anything is replaced. Never a blind `git pull`; never executes downloaded code.
- **Update awareness**: Aurora's prompt carries a short software-state block (version,
  update availability, last upgrade outcome) so she can talk naturally about her own
  state; the last five upgrade records surface in `/api/status` and Settings.
- Live end-to-end verification scripts: `scripts/verify_reminders_e2e.py` (real server,
  real waits: chat → store → SSE delivery → bell → repeat/snooze/dismiss → restart
  persistence) and `scripts/verify_update_e2e.py` (real server + local fake-GitHub:
  check → install → restart → verify → rollback → corrupt-download refusal).
- `Database.backup_to()` (online SQLite snapshot) and the `update.api_base` config key
  (GitHub Enterprise / Gitea compatible).

### Fixed
- Time parsing: bare durations ("15 minutes", "2 hours 30 mins", "for 5 minutes")
  now parse the same as "in 15 minutes" everywhere — chat, dashboard form, API.
- Reminder data quality: greetings/test noise ("hi", "null", blanks) are rejected at
  the store — the single choke point every creation path flows through — so junk
  rows can no longer be stored; the Reminders table also guards its rendering.
- Timer snooze: a fired (delivered) one-shot timer can be brought back via the new
  `ReminderStore.bring_back()` (plain `snooze()` still only covers active statuses);
  re-firing a snoozed timer keeps its ORIGINAL duration for repeats.
- Updater restart: the script path is resolved at startup and the process chdirs to
  the new tree before `execv` (the cwd is swapped out from under the running process),
  so an update restart works after the tree swap.
- Voice announcements are awaited by the delivery paths (previously fire-and-forget,
  an utterance could be cut off when the turn ended first).

### Fixed (updater hardening — Portable deployment)
- **Portable systemd permissions (confirmed + fixed):** under the installed unit
  (`ProtectSystem=strict` + `NoNewPrivileges=true`) the updater could not write
  `/opt/aurora-companion` at all and could not elevate — `apply()` failed at the swap
  step on every Portable install. It now runs in an **assisted mode**: backup,
  download and staging happen inside the writable data directory, and a strictly
  validated manifest is handed to the new `aurora-update-helper` (root-only, fixed
  script) via the one-shot `aurora-update.service`, which the dashboard may start
  through a narrowly scoped polkit rule (start `aurora-update.service` only — no
  other unit, no other verb). Without polkit, the dashboard shows the exact manual
  command with the release already staged, verified and backed up.
- **Rollback destroyed the virtualenv (confirmed + fixed):** the backup archive
  excludes `venv/` and `.git/` by design, but the restore recreated the tree without
  carrying them — a rollback left Aurora unable to restart. Local state (`venv`,
  `.git`, `config.yaml`, `aurora.env`) is now carried aside before the restore and
  moved back afterwards, in both direct and helper modes.
- Backups now store normalized modes (dirs 750 / files 644): a backup captured while
  the tree was read-only must stay extractable and runnable afterwards.
- The helper consumes its manifest after applying it, so a later rollback cannot
  collide with the old install manifest; both manifests pending is refused for human
  review.
- `restart_soon()` no longer depends on a (possibly absent or externally managed)
  asyncio event loop — the delayed `execv` restart runs on a daemon thread.
- Helper manifest validation refuses path escapes: staging and backup must resolve
  inside the service-writable data directory.

### Added (updater hardening — Portable deployment)
- `deploy/aurora-update-helper` (validate / install / rollback / apply),
  `deploy/aurora-update.service` (oneshot root helper) and
  `deploy/aurora-update.polkit` (start-only authorization for the service user);
  `deploy/install.sh` installs all three.
- `scripts/verify_portable_update_e2e.py` — reproduces the Portable restrictions at
  the permission level (the sandbox has no systemd, so this is a faithful
  reproduction of the filesystem effect, **not** a Portable deployment test):
  read-only apply → staged manifest (nothing touched) → helper install with
  venv/git/config/env carried → restart → verification → rollback with the venv
  usable → restart → data intact. **28/28 checks passed**; the run found and fixed
  the helper's missing carry directory, the manifest-consumption bug and the
  unwritable-backup bug.
- Updater unit tests grew to 13 (carry-across install, venv-preserving rollback,
  rollback after an interrupted update, read-only staging, helper manifest
  validation incl. path-escape refusal); suite now **265 passing**.
- Docs: `docs/PORTABLE_DEPLOY.md §3b` (assisted updates on Portable) and a
  GitHub-release packaging note that `Aurora-Companion.zip` is REQUIRED for
  one-button updates (a tag-only release cannot be installed by the dashboard).

### Added (previous work)
- GitHub release package: rewritten README with genuine dashboard screenshots
  (captured from the fictional demo database), conceptual logo + social-preview
  artwork, `docs/GITHUB_RELEASE.md`, `CONTRIBUTING.md`, an extended `.gitignore`,
  and a demo-data generator (`scripts/make_demo_db.py`) that seeds fictional
  conversations, facts and reminders through Aurora's real APIs.
- Mock provider's default reply is now a clearly labeled offline notice instead of
  the terse "(mock: no matching rule)".

### Fixed (deployment, Ubuntu Portable)
- `deploy/install.sh` now installs `aurora.env` **before** the permission block and
  the pre-start verification, applying explicit `0640 root:aurora` on clean installs
  and reinstall (the file was previously copied after both, skipping the mode).
- TLS permission policy made internally consistent: `deploy/make-cert.sh` writes the
  certificate AND key `0640 root:aurora` in a `0750 root:aurora` directory (the
  certificate was previously `0644`, conflicting with the verifier's no-world-access
  rule); `deploy/verify-permissions.sh` gained explicit TLS checks.
- `deploy/verify-voice.sh` now applies the local self-signed-HTTPS handling (`-k`) to
  the unauthenticated HTTP-401 probe too, and trusts a received HTTP status even if
  the transport closes rudely afterwards.

## [0.9.0-rc.2] — 2026-11-21 (internal, pre-release deployment hardening)

### Fixed
- Service permissions on Ubuntu Portable: `/etc/aurora` and `config.yaml` are now owned
  by the non-root service account (`750` dir, `600` config) so it can read the config
  and persist preferences (voice selection) while the auth token stays hidden from
  other users; `aurora.env` is root-managed `0640 root:aurora`.
- The installer verifies every relevant path **as the service account** (runuser) and
  smoke-tests startup + configuration persistence before starting the systemd unit.
- systemd hardening tightened: `ProtectSystem=strict`, application tree read-only,
  only data + config writable, bytecode caching disabled.
- `deploy/verify-voice.sh`: auth header passed as a proper Bash array (`AUTH_ARGS`),
  HTTP-401 rejection check via captured `%{http_code}`, embedded Python rewritten for
  Python 3.8–3.11 compatibility (old f-string escapes were a SyntaxError), token never
  printed.
- Secure LAN push-to-talk: optional HTTPS on Aurora's own port (`server.tls_cert/key`,
  `deploy/make-cert.sh`, `install.sh --tls`); the dashboard disables the microphone
  with an explanatory hint in insecure contexts.

### Added
- `deploy/make-cert.sh` and `deploy/verify-permissions.sh`.
- 33 deployment regression tests (unit content, installer permission commands,
  verify-script behavior, permission-model round-trip, TLS handling, a live
  self-signed-HTTPS checklist run).

## [0.9.0-rc.1] — 2026-11-21 (internal, Portable milestone)

### Added
- Kayock voice integration: TTS via the existing OpenAI-compatible speech adapter with
  live voice-id discovery and dashboard selection; STT via the existing
  Faster-Whisper transcription service; honest degradation when they are down.
- Resource-aware operation: ComputeGuard (model health, foreground grace, GPU
  traffic-light probe, kill switch) defers background AI; user chat always wins.
- Ubuntu deployment: `deploy/install.sh` (port-collision checks, generated auth token,
  systemd unit), `deploy/backup.sh` (online SQLite backup), `deploy/verify-voice.sh`,
  LAN token auth on `/api/*` with the public dashboard page.
- Dashboard voice UI, privacy permission registry, people; 74 new tests.

## [0.1.0] — 2026-11-21 (internal, initial build)

### Added
- Unified companion core rebuilt from Luna Local Agent + Shikigami Protocol (see
  NOTICE.md): conversation manager with rolling summaries and the tool loop,
  MemoryStore (bi-temporal facts, FTS5 + vector recall, consolidation, supersession,
  tombstones), ReminderStore (sleep-until-due scanner, missed-reminder recovery),
  ProactiveScheduler (urgency gates, quiet hours, caps, cooldowns), PersonaEngine
  (seed + evolved persona, two-dial mood engine, relationship tiers), OpenAI-compatible
  and mock providers, single-page dashboard.
- Real-model integration run against a live GLM endpoint (transcript in
  docs/integration_transcript.md).

[Unreleased]: https://github.com/kayock/Aurora-Companion/compare/v0.9.0-rc.2...HEAD
[0.9.0-rc.2]: https://github.com/kayock/Aurora-Companion/compare/v0.9.0-rc.1...v0.9.0-rc.2
[0.9.0-rc.1]: https://github.com/kayock/Aurora-Companion/compare/v0.1.0...v0.9.0-rc.1
[0.1.0]: https://github.com/kayock/Aurora-Companion/releases/tag/v0.1.0
