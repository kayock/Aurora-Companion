# GitHub Release Guide — Aurora Companion

Everything needed to publish this repository as a clean, professional GitHub project.
This document is for the maintainer; it does not affect the application.

## 1. Suggested repository name

**`Aurora-Companion`** under the existing `kayock` organization:

```
https://github.com/kayock/Aurora-Companion
```

(The name is proposed, not assumed to exist. Alternatives: `aurora-companion`,
`aurora-local-companion`.)

## 2. About-section description (≤ 350 characters)

> A local-first AI companion: one app uniting an evolving personality, persistent
> evidence-backed memory, autonomous-but-polite initiative, model-free reminders, and
> push-to-talk voice against any OpenAI-compatible model. Experimental, AGPL-3.0,
> runs on your own machine. (293 characters)

Shorter alternative: `One cohesive local AI companion — evolving personality, persistent
memory, its own initiative, voice. Experimental, AGPL-3.0, yours.` (129 characters)

## 3. Suggested topics

```
ai-companion  local-llm  llm  fastapi  sqlite  personal-assistant
agent  memory  personality  reminders  tts  speech-to-text
openai-compatible  self-hosted  agpl  python
```

(GitHub allows up to 20 topics, lowercase, no spaces.)

## 4. Recommended repository settings

- **Default branch**: `main`.
- **Description + topics** from §2/§3; website field optional.
- **Social preview**: upload `docs/assets/aurora-social-preview.png`
  (Settings → General → Social preview; 1280×640 as generated).
- **Releases**: enable; use the checklist in §5 for v0.9.0.
- **Issues**: enable with these labels: `bug`, `enhancement`, `documentation`,
  `on-site-verification` (for hardware/service checks that cannot run in CI),
  `good first issue`.
- **Discussions**: optional; Q&A category for setup help keeps issues clean.
- **Pull requests**: enable; template from `CONTRIBUTING.md` works as-is.
- **Actions**: not required (the test suite is pure pytest and runs locally); if added
  later, a simple `pytest` workflow is enough — no secrets needed.
- **Branch protection**: protect `main` (require PR + passing checks once CI exists).
- **Do not** enable "Template repository"; Aurora carries state assumptions.
- **Security**: enable private vulnerability reporting; the `SECURITY.md` file is not
  required to start but can be added later.

## 5. Release checklist

1. `python -m pytest tests/ -q` → all green (232 expected).
2. `bash -n deploy/*.sh` → no syntax errors (also covered by the suite).
3. `python scripts/make_demo_db.py /tmp/aurora-demo` → dashboard opens, demo data shows.
4. Docs link check: every relative link in README/docs resolves (all links point to
   files that exist in this repo).
5. `git status --porcelain` clean; no `config.yaml`, `*.db`, `*.crt/*.key`, `data-demo/`,
   logs or venv committed (`.gitignore` guards these; double-check with
   `git ls-files | grep -E '\.db|config\.yaml|\.crt|\.key|data-demo'` → must be empty).
6. Tag and push: `git tag -a v0.9.0 -m "Aurora Companion v0.9.0 — first public release"`
   then `git push origin main --tags`.
7. Draft the GitHub release from the tag with the notes in §7; attach the built
   `Aurora-Companion.zip`. The zip is REQUIRED for Aurora's one-button updater
   (it downloads exactly that asset name; a tag-only release cannot be installed
   through the dashboard).
8. Upload the social preview image (§4).
9. After publishing: pin the README, verify the About description/topics, and check the
   repository renders the screenshots (paths are relative and case-sensitive).

## 6. Initial versioning recommendation

**`v0.9.0`** — "release candidate": the application is feature-complete for its
design (personality evolution, memory, reminders, proactive loop, voice adapters,
resource-aware operation, hardened deployment), with 232 automated tests, but real-world
mileage on live audio hardware and long-running personality evolution is still being
gathered. CalVer or date-based versions would overstate stability; semver fits:

- `v0.9.x` — release-candidate fixes.
- `v1.0.0` — after a period of on-machine validation (voice hardware pass, long-run
  personality evolution, LAN push-to-talk in daily use), documented in
  `docs/TEST_RESULTS.md`.

## 7. Suggested first release title and notes

**Title:** `Aurora Companion v0.9.0 — first public release`

**Notes:**

```markdown
Aurora is one cohesive, locally powered AI companion — one identity, one memory, one
conversation, one proactive scheduler, one dashboard — rebuilt from the strongest
capabilities of Luna Local Agent and Shikigami Protocol (both AGPL-3.0).

Highlights
- Evolving personality with evidence-gated evolution, core anchor, changelog and rollback
- Persistent memory: bi-temporal facts, FTS5 + optional vector recall, consolidation,
  supersession, forget-tombstones
- General-purpose reminders (one-time / recurring / nag) that never need the model
- Proactive initiative with urgency gates, quiet hours, focus mode, caps and cooldowns
- Voice: push-to-talk STT (Faster-Whisper service), TTS via OpenAI-compatible adapters
  (e.g. Kayock) with live voice discovery; explicit browser + app permissions only
- Resource-aware: background AI defers when compute is needed elsewhere
- Hardened Ubuntu deployment: non-root systemd service, verified permission model,
  optional LAN HTTPS for push-to-talk
- 232 automated tests; built-in mock provider + fictional demo database for offline trials

Status
Experimental (v0.9.0 release candidate). Audio I/O and live services are verified on
target machines, not in CI — see docs/TEST_RESULTS.md and docs/LIMITATIONS.md.

License: AGPL-3.0-or-later (see NOTICE.md for upstream attribution).
```

## 8. Uploading and publishing the existing repository

The repository already exists locally with its full history (do not recreate or squash
it — the history is part of the record):

```bash
# 1. from the project root
git remote add origin git@github.com:kayock/Aurora-Companion.git
#    (or https://github.com/kayock/Aurora-Companion.git)

# 2. sanity-check what would be published
git ls-files | grep -E '\.db|config\.yaml$|\.crt$|\.key$|data-demo|\.log$'   # expect: empty
git log --oneline | head            # history intact

# 3. push (this publishes the code — do it deliberately, with authorization)
git push -u origin main

# 4. follow §5 from step 6 for the tag + release
```

If the GitHub repo must be initialized from scratch instead, create it **empty**
(no README/.gitignore/license scaffolding — those exist here) and push; an empty target
cannot conflict with the local history.

What must never be pushed: any `config.yaml` with a real token, any `*.db` (real
conversations/memory), any `*.crt`/`*.key`, anything under `data/` or `data-demo/`.
`.gitignore` blocks these patterns; the §5 check verifies it before every push.
