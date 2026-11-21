#!/usr/bin/env python3
"""Intelligence Lab CLI — run the fair model benchmark.

Local evaluation (needs the Ollama endpoint with the models already present —
this harness never downloads, installs or changes models):

    python scripts/run_intelligence_lab.py \
        --models llama3.2:3b,phi4-mini:latest,deepseek-q8:latest

LM Studio / llama.cpp / vLLM style endpoints:

    python scripts/run_intelligence_lab.py --backend openai \
        --base-url http://127.0.0.1:1234 --models local-model

Offline pipeline validation (no server at all — scripted mock that
exercises empty answers, thinking-only answers and an integration error):

    python scripts/run_intelligence_lab.py --mock --trials 2

Results land in --out (default: ./reports/intelligence_lab/<timestamp>):
report.md (readable tables) and report.json (full data for further analysis).

The runner is DB-free: it cannot touch Aurora's production conversation,
memories, persona or model configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aurora.evaluation import (  # noqa: E402
    IntelligenceLab, MockEvalBackend, OllamaBackend, OpenAICompatBackend,
    build_scenarios, export_report, probe_backend,
)

DEFAULT_MODELS = ["llama3.2:3b", "phi4-mini:latest", "deepseek-q8:latest"]


def mock_handler(scenario_id: str, turn_no: int, trial_no: int,
                 messages, tools):
    """Deterministic mock script exercising every accounting path:
    correct answers, one empty-with-thinking run, one fully empty run,
    one integration error, and scripted tool calls."""
    from aurora.evaluation import Completion, IntegrationError

    if scenario_id == "conv_smalltalk_flow" and trial_no == 2 and turn_no == 2:
        # DeepSeek-style failure: reasoning exists, visible answer does not
        return Completion(visible="", thinking="The user mentioned grind size...",
                          timing={"wall_s": 2.0, "generation_s": 1.8,
                                  "prompt_eval_s": 0.2, "ttft_s": 0.2,
                                  "tokens_per_s": 30.0})
    if scenario_id == "conv_humor" and trial_no == 2:
        return Completion(visible="", thinking="",
                          timing={"wall_s": 1.0, "tokens_per_s": 20.0})
    if scenario_id == "conv_nuance_tone" and trial_no == 3:
        raise IntegrationError("simulated endpoint outage")
    if scenario_id == "tool_right_tool_right_args" and turn_no == 0 and tools:
        import json as _json
        return Completion(visible="", tool_calls=[{
            "name": "get_weather",
            "arguments": _json.dumps({"city": "Oslo"})}],)
    if scenario_id == "tool_restraint_chat" and tools:
        return Completion(visible="Honestly, Alex? I love it — the world goes "
                                  "quiet when snow falls.")
    if scenario_id == "tool_no_unauthorized" and tools:
        return Completion(visible="I can't do that — there is no note-deletion "
                                  "capability in my toolset, and I wouldn't "
                                  "bulk-delete your data on a bare request.")
    if scenario_id == "tool_malformed_result" and tools and messages \
            and messages[-1].get("role") == "tool":
        return Completion(visible="The weather service returned a malformed "
                                  "response, so I couldn't get Oslo's weather. "
                                  "Want me to try again?")
    if scenario_id == "tool_malformed_result" and tools:
        import json as _json
        return Completion(visible="", tool_calls=[{
            "name": "get_weather", "arguments": _json.dumps({"city": "Oslo"})}])
    if scenario_id == "tool_right_tool_right_args" and messages \
            and messages[-1].get("role") == "tool":
        return Completion(visible="It's currently light snow and about 3°C in "
                                  "Oslo, Alex — bundle up.")
    if scenario_id == "reason_multistep_time":
        return Completion(visible="First, add 30 minutes to 14:20 → 14:50. "
                                  "Then the 5-minute wait → 14:55. Then 45 more "
                                  "minutes of travel → 15:40. So the train "
                                  "arrives at 15:40.")
    if scenario_id == "reason_uncertainty":
        return Completion(visible="I have no way to know what you had for "
                                  "breakfast yesterday, Alex — you haven't "
                                  "told me.")
    if scenario_id == "mem_recall_in_context" and turn_no == 2:
        return Completion(visible="Your sister is Marta, and she lives in Oslo, Alex.")
    if scenario_id == "mem_correction" and turn_no == 2:
        return Completion(visible="You live in Bergen now, Alex.")
    if scenario_id == "recov_accept_no" and turn_no == 1:
        return Completion(visible="No problem, Alex.")
    if scenario_id == "pers_respect_feedback" and turn_no == 1:
        return Completion(visible="Oslo, Alex.")
    if scenario_id == "tool_incomplete_request" and tools:
        return Completion(visible="Which city would you like the weather for, Alex?")
    return (f"Here's a thoughtful mock answer for turn {turn_no + 1}, Alex — "
            "on-topic and emoji-free.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Aurora Intelligence Lab")
    p.add_argument("--models", default=",".join(DEFAULT_MODELS),
                   help="comma-separated model ids (default: the three local models)")
    p.add_argument("--backend", choices=["ollama", "openai"], default="ollama",
                   help="ollama native API or an OpenAI-compatible endpoint")
    p.add_argument("--base-url", default="http://127.0.0.1:11434",
                   help="endpoint base URL (Ollama default :11434)")
    p.add_argument("--api-key", default="",
                   help="optional bearer token for OpenAI-compatible endpoints "
                        "(local servers usually need none)")
    p.add_argument("--trials", type=int, default=3, help="trials per scenario")
    p.add_argument("--temperature", type=float, default=0.7,
                   help="identical for every model in the run")
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--filler-turns", type=int, default=14,
                   help="length of the long-context scenario transcripts")
    p.add_argument("--categories", default="",
                   help="comma-separated category filter (optional)")
    p.add_argument("--out", default="", help="output directory")
    p.add_argument("--mock", action="store_true",
                   help="offline pipeline validation with scripted answers")
    p.add_argument("--list-scenarios", action="store_true")
    return p


async def async_main(args) -> int:
    scenarios = build_scenarios(context_turns=args.filler_turns)
    if args.list_scenarios:
        for s in scenarios:
            print(f"[{s.category}] {s.id} — {s.title} ({s.n_turns} turns)")
        return 0
    cats = {c.strip() for c in args.categories.split(",") if c.strip()}
    if cats:
        scenarios = [s for s in scenarios if s.category in cats]
        if not scenarios:
            print(f"no scenarios match {sorted(cats)}", file=sys.stderr)
            return 2

    if args.mock:
        models = ["mock-small", "mock-thinky", "mock-empty"]
        factory = lambda m: MockEvalBackend(m, handler=mock_handler)
    else:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
        ok = await probe_backend(args.base_url, args.backend)
        if not ok:
            print(f"ERROR: endpoint {args.base_url} is not reachable. "
                  "Start the local server, or use --mock to validate the "
                  "pipeline offline. This harness never downloads models.",
                  file=sys.stderr)
            return 2
        if args.backend == "ollama":
            factory = lambda m: OllamaBackend(m, base_url=args.base_url)
        else:
            factory = lambda m: OpenAICompatBackend(m, base_url=args.base_url,
                                                    api_key=args.api_key)

    lab = IntelligenceLab(factory, scenarios=scenarios, trials=args.trials,
                          temperature=args.temperature,
                          max_tokens=args.max_tokens,
                          filler_turns=args.filler_turns)
    print(f"Intelligence Lab: {len(models)} model(s) × {len(scenarios)} "
          f"scenario(s) × {args.trials} trial(s) …")
    report = await lab.run(models)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) if args.out else \
        ROOT / "reports" / "intelligence_lab" / stamp
    paths = export_report(report, out_dir)
    print("\n=== Ranking ===")
    for r in report.ranking:
        print(f"{r['rank']}. {r['model']}: intelligence "
              f"{r['intelligence_score'] * 100:.1f} · completion "
              f"{r['visible_completion_rate'] * 100:.0f}% · tools "
              f"{r['tool_success_rate'] if r['tool_success_rate'] is not None else '—'} · "
              f"{r['mean_tokens_per_s'] or '—'} tok/s")
    print(f"\nReport: {paths['markdown']}\nJSON:   {paths['json']}")
    return 0


def main() -> int:
    return asyncio.run(async_main(build_parser().parse_args()))


if __name__ == "__main__":
    sys.exit(main())
