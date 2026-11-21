# Integration instructions for Cursor

**Baseline:** `c07de322b53fc76e3680c56db6ec03d2075ec1bb` (Aurora 1.0.6) — verified

**1.0.7 note:** `launcher.enabled` ships **false**. The ZIP defaulted it to true; that was changed so a deploy does not open programs until the owner opts in.
before development; every change below is relative to that commit.

**Scope:** two features, no rewrites, no production-data changes, no model
downloads/selection changes, no new dependencies, no auth changes, no
GitHub push or release, no deployment.

## Changed files (manifest vs `c07de322`)

New (13):
| File | Contents |
|---|---|
| `aurora/launcher.py` | catalog-only launch service: parsing, matching, argv `Popen`, URL scheme validation, availability, intent gating |
| `aurora/evaluation/__init__.py` | Intelligence Lab public API |
| `aurora/evaluation/models.py` | data model + trial states (empty/thinking/integration-error semantics) |
| `aurora/evaluation/backends.py` | Ollama / OpenAI-compat / Mock backends, thinking-channel split, context+memory inspection |
| `aurora/evaluation/scenarios.py` | 31 multi-turn scenarios, 7 weighted categories, fixed eval persona |
| `aurora/evaluation/scoring.py` | objective rubric graders |
| `aurora/evaluation/runner.py` | orchestration, tool loop, aggregation, ranking |
| `aurora/evaluation/report.py` | markdown + JSON report export |
| `tests/test_launcher.py` | 35 tests |
| `tests/test_evaluation.py` | 23 tests |
| `scripts/run_intelligence_lab.py` | Intelligence Lab CLI |
| `docs/APP_LAUNCHING.md` | feature + destination semantics + safety |
| `docs/INTELLIGENCE_LAB.md` | methodology, scoring, run instructions |

Modified (7):
| File | Change |
|---|---|
| `aurora/conversation/manager.py` | deterministic launch hook `_try_launch_request` in the turn flow (+1 import, +1 ctor param); `_looks_like_tool_json` extended with the 3 new tool names |
| `aurora/conversation/tools.py` | 3 launch tools registered (intent-gated like the reminder tools) |
| `aurora/app.py` | `Launcher` built + wired into the registry and manager |
| `aurora/server/app.py` | read-only `GET /api/launcher` endpoint |
| `aurora/config.py` | `launcher.*` defaults (catalog with common Ubuntu apps/sites) |
| `config.example.yaml` | documented `launcher:` section |
| `CHANGELOG.md` | entry for this change set |

(Plus `INTEGRATION.md` itself; the exact `git diff --name-status c07de322`
list ships as `integration_manifest.txt` inside the ZIP.)

## Integration steps

1. Unpack over a working tree at `c07de322` (or `git apply` / copy the
   listed files — the ZIP preserves the full project structure).
2. `python -m pytest` → expect **405 passed** (baseline was 347).
3. Start Aurora and try in chat: "open firefox", "open github",
   "what applications can you open?", "open firefox on my computer"
   (must refuse honestly). Adjust `launcher:` in `config.yaml` to the
   machine's real applications (defaults include firefox/chromium/cursor/
   code/files/terminal/calculator/text editor/settings/system monitor;
   add the Saturday Multiverse site URL).
4. Model evaluation, on the host that has the models (Ollama):
   `python scripts/run_intelligence_lab.py --models llama3.2:3b,phi4-mini:latest,deepseek-q8:latest --trials 3`
   → read `reports/intelligence_lab/<ts>/report.md`. See
   `docs/INTELLIGENCE_LAB.md` ("What must be run locally").

## Guarantees kept

- Existing conversation/personality/memory/reminder/voice architecture
  untouched; all existing tests pass unchanged.
- No production DB/config writes; Intelligence Lab opens no database at all.
- No new dependencies (httpx/pytest already required), no new auth, no new
  background services (the launcher is request-driven; the lab is a CLI).
- No Kayock Voice, TLS, or updater changes.
