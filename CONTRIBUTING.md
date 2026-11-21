# Contributing to Aurora Companion

Thank you for considering a contribution. Aurora is an experimental, AGPL-3.0 licensed
local AI companion — pragmatic, honest about its limits, and deliberately free of cloud
requirements.

## Ground rules

1. **AGPL-3.0-in, AGPL-3.0-out.** Every contribution is licensed under
   [AGPL-3.0-or-later](LICENSE). Do not paste code whose license is incompatible.
   Upstream attribution obligations live in [NOTICE.md](NOTICE.md) — if your change
   derives from Luna Local Agent or Shikigami Protocol behavior, note it in the PR so
   the comparison doc stays truthful.
2. **One of everything that should be one of X.** Aurora's architecture is built around
   single instances: one store, one conversation, one proactive scheduler, one persona
   engine. Please do not add competing subsystems; extend the existing ones.
3. **Honesty over polish.** Degraded behavior must be reported, not hidden; features
   must not be claimed beyond their tests (see docs/TEST_RESULTS.md for the current
   boundary).
4. **No required new dependencies** for core operation. The demo and tooling scripts
   must stay stdlib-only where possible.

## Getting started

```bash
git clone https://github.com/kayock/Aurora-Companion.git
cd Aurora-Companion
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests/ -q          # should be all green before you start
python scripts/make_demo_db.py      # fictional demo data for the dashboard
python run.py --config data-demo/config.yaml
```

Python 3.10+ recommended. No model server, no voice services, no network needed for
development — the mock provider and the demo database cover the whole interface.

## Workflow

1. Fork / branch from `main` (`feat/<topic>` or `fix/<topic>`).
2. Make the change, with tests:
   - behavior changes need a test that fails before and passes after;
   - bug fixes need a regression test reproducing the bug;
   - pure-docs changes are exempt.
3. `python -m pytest tests/ -q` and `bash -n deploy/*.sh` must pass.
4. Keep diffs focused — no drive-by reformatting; match the existing style
   (stdlib-first, type hints on public functions, defensive parsing of model output).
5. Open a pull request describing **what** changed, **why**, and **what is tested**.
   If a change affects deployment (`deploy/`, systemd unit, permissions), call it out —
   those changes are validated against the permission model and must keep the
   non-root service account able to read and persist its configuration.
6. Commits should read as complete sentences in the imperative ("Fix X", not "fixed X").
   Squash-or-merge is fine either way.

## What particularly needs help

- **On-site verification** of the audio path on real hardware (microphones, speakers,
  live Faster-Whisper/speech-adapter binaries) — documented in
  [docs/TEST_RESULTS.md](docs/TEST_RESULTS.md) as the boundary between automated
  coverage and reality; field reports and test notes are valuable.
- **Personality evolution mileage**: long-running observations of evolution quality,
  baseline review, and rollback behavior.
- **Traffic-light protocols**: if you run GPU coordination on your machine, share the
  JSON shapes so `resources.busy_markers` presets can be added.
- **Documentation** — especially non-Ubuntu deployment notes.

## Reporting issues

Please include: Aurora version/commit, OS + Python version, whether a real model server
was involved, relevant config **without tokens** (`server.auth_token` must never be
posted), the dashboard tab or CLI command involved, and logs from
`journalctl -u aurora-companion` (systemd) or the console (manual run). Redact personal
memory contents — a description of the shape of the data is enough.
