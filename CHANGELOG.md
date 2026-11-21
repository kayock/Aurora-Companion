# Changelog

All notable changes to Aurora Companion. The project follows semver-ish versioning
(see docs/GITHUB_RELEASE.md §6). Dates are UTC.

## [Unreleased]

### Added
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
