# Aurora Intelligence Lab

A repeatable, fair benchmark for choosing which **local** model makes Aurora
the most intelligent and engaging companion. Intelligence is the primary
score; speed is measured and reported but never blended into it.

    python scripts/run_intelligence_lab.py \
        --models llama3.2:3b,phi4-mini:latest,deepseek-q8:latest

## What it evaluates — 31 scenarios, 7 categories

| Category | Weight | What it covers |
|---|---:|---|
| Conversational intelligence | 1.0 | multi-turn flow, humor, tone/sarcasm, topic tracking, non-repetition |
| Reasoning | 1.5 | multi-step arithmetic with explanation, causality, contradictions, uncertainty, syllogisms |
| Memory & continuity | 1.25 | in-context recall, corrections, user-vs-model claim attribution, no fabricated memories, retrieval among distractors |
| Personality development | 1.0 | persona consistency (name use, no emoji), respecting feedback ("keep replies short"), format preference learning, no scripted repetition |
| Tool use | 1.25 | right tool, valid arguments, using tool results, restraint (no call for opinions), refusing unauthorized actions, malformed tool results, incomplete requests |
| Context reliability | 1.0 | exact retrieval and summarization over long conversations |
| Conversation recovery | 1.25 | accepting "No.", topic changes, misunderstanding repair, tool errors, ambiguous requests |

Every scenario is a multi-turn conversation script with objective checks per
turn (`aurora/evaluation/scenarios.py`). The same fixed `EVAL_PERSONA`
system prompt is used for every model — isolated from Aurora's real persona,
mood, memories and databases (the harness opens **no database at all**).

## Fairness rules (why this run is trustworthy)

The previous five-scenario comparison had a structural flaw: **empty visible
responses were counted as passes**. This harness fixes that:

1. **Empty visible answers can never pass.** A turn whose *visible* text is
   empty scores 0 — every grader runs against the visible text only.
2. **Reasoning/thinking channels are separate.** Ollama's `message.thinking`
   and DeepSeek-style `reasoning_content`/inline `<think>` blocks are
   recorded for inspection but never treated as an answer and never re-fed
   into history. Reasoning-only replies are their own state
   (`empty_with_thinking`) so the DeepSeek Q8 failure mode is *visible*, not
   silently folded into "wrong answer".
3. **Integration failures are not model failures.** Endpoint down, HTTP 5xx,
   timeouts → `IntegrationError`, excluded from all scores and reported in
   the diagnostics section.
4. **Identical prompts and settings.** Same persona, same scripts, same
   temperature (default 0.7) and max_tokens for every model in a run —
   asserted in tests.
5. **Repeated trials** (default 3) per scenario; means reported per scenario.
6. **Effective context is read, never changed.** `inspect()` reads
   `num_ctx` and architecture context lengths from `/api/show` (and
   `/api/ps` for memory); scenarios that cannot fit a model's effective
   window are **skipped honestly**, not truncated.
7. **No model judges another model.** Scoring is rubric-based: exact facts,
   numbers, tool-call structure, uncertainty markers, repetition ratios.
   Tone/humor checks are heuristic and flagged as judgment criteria in the
   report.

## Scoring

- Per turn: fraction of applicable checks passed (objective rubric).
- Per scenario trial: mean of turn scores; empty/reasoning-only trial = 0.
- Per scenario: mean over scored trials (completed + empty; integration
  errors excluded) and a completion rate.
- Per category: mean of scenario scores.
- **Intelligence score**: weighted category mean (weights above).
- Reported separately: visible completion rate, tool-call success rate,
  and speed diagnostics — prompt eval time, time-to-first-token, generation
  speed, total wall time, loaded-model memory, effective context, and the
  empty-answer rate.

## Running

Local evaluation (Ollama must already have the models — the harness never
downloads, installs, removes or changes models, and never touches Aurora's
production model selection):

    python scripts/run_intelligence_lab.py \
        --models llama3.2:3b,phi4-mini:latest,deepseek-q8:latest \
        --trials 3

LM Studio / llama.cpp / vLLM style endpoints:

    python scripts/run_intelligence_lab.py --backend openai \
        --base-url http://127.0.0.1:1234 --models local-model

Offline pipeline validation (no server needed; scripted mock exercises
correct answers, empty-with-thinking, fully empty, an integration error and
the full tool loop):

    python scripts/run_intelligence_lab.py --mock --trials 3

Useful flags: `--list-scenarios`, `--categories reasoning,memory`,
`--filler-turns 14` (length of long-context transcripts), `--out DIR`.

Output: `reports/intelligence_lab/<timestamp>/report.md` (readable tables:
ranking, per-model category/scenario detail, empty-answer list, integration
issues) and `report.json` (full machine-readable data, including every
check's observed evidence).

## What must be run locally (by Cursor, on the Portable/LM Studio host)

This sandbox has **no Ollama server and no model weights**; per the no-
fabrication rule, **no model scores are claimed**. The mock run validates
the pipeline end-to-end. Cursor must run, on the real host:

1. `python scripts/run_intelligence_lab.py --models llama3.2:3b,phi4-mini:latest,deepseek-q8:latest --trials 3` (Ollama on 127.0.0.1:11434 with those models present), and
2. optionally the same via `--backend openai --base-url <LM Studio>` for the server Aurora actually uses in production.

Then compare `report.md` rankings: pick the highest intelligence score with
an acceptable visible completion rate (a model that answers < 90% of turns
visibly is unreliable as a companion regardless of score). Aurora's
production model is **not** changed by this task — switching remains the
owner's explicit decision (Settings → Models).

## Module map

- `aurora/evaluation/models.py` — data model + trial states
- `aurora/evaluation/backends.py` — Ollama / OpenAI-compat / Mock backends,
  thinking-channel splitting, context+memory inspection
- `aurora/evaluation/scenarios.py` — the 31 scenarios, weights, persona
- `aurora/evaluation/scoring.py` — objective check graders
- `aurora/evaluation/runner.py` — orchestration, tool loop, aggregation,
  ranking
- `aurora/evaluation/report.py` — markdown + JSON export
- `scripts/run_intelligence_lab.py` — CLI
- `tests/test_evaluation.py` — 23 tests (fair accounting, channels,
  isolation, tools, export, DB-free guarantee)
