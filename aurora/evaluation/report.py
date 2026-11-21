"""Readable Intelligence Lab reports: markdown + JSON export.

The markdown keeps three things visually separate, per the fairness rules:
  1. the intelligence ranking (weighted category scores only),
  2. reliability (visible completion rate, tool success, integration errors),
  3. speed diagnostics (never blended into the intelligence score).
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import (
    STATE_EMPTY, STATE_EMPTY_WITH_THINKING, STATE_INTEGRATION_ERROR,
    ModelReport, RunReport,
)
from .scenarios import CATEGORY_TITLES, CATEGORY_WEIGHTS


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def _num(x, nd=2):
    return "—" if x is None else f"{x:.{nd}f}"


def ranking_table(report: RunReport) -> str:
    lines = [
        "| Rank | Model | Intelligence | Visible completion | Tool success | Effective ctx |",
        "|---:|---|---:|---:|---:|---:|"]
    for r in report.ranking:
        lines.append(
            f"| {r['rank']} | {r['model']} | {_num(r['intelligence_score'] * 100, 1)} | "
            f"{_pct(r['visible_completion_rate'])} | {_pct(r['tool_success_rate'])} | "
            f"{r.get('effective_context') or '—'} |")
    return "\n".join(lines)


def speed_table(m: ModelReport) -> str:
    s = m.speed
    return (f"tokens/s {_num(s.get('mean_tokens_per_s'))} · "
            f"first-token ≈ {_num(s.get('mean_ttft_s'))}s · "
            f"prompt eval {_num(s.get('mean_prompt_eval_s'))}s · "
            f"generation {_num(s.get('mean_generation_s'))}s · "
            f"total wall {_num(s.get('mean_wall_s'))}s")


def context_table(m: ModelReport) -> str:
    ci = m.context_info
    if ci.get("error"):
        return f"inspection failed: {ci['error']}"
    adv = ci.get("advertised_context_lengths") or []
    return (f"advertised: {', '.join(map(str, adv)) or '—'} · "
            f"num_ctx parameter: {ci.get('num_ctx_parameter') or '—'} · "
            f"effective: {ci.get('effective_context') or '—'} · "
            f"loaded size: {ci.get('loaded_size_bytes') or '—'} bytes")


def scenario_table(m: ModelReport) -> str:
    lines = ["| Scenario | Category | Score | Completion | States |",
             "|---|---|---:|---:|---|"]
    for sid in sorted(m.scenario_results):
        sres = m.scenario_results[sid]
        states = " ".join(
            {"completed": "✓", "empty_visible": "∅",
             "empty_with_thinking": "🤔", "integration_error": "⚠"}
            [t.state] for t in sres.trials) or "—"
        lines.append(f"| {sid} | {sres.category} | "
                     f"{_num(sres.mean_score * 100, 0)} | "
                     f"{_pct(sres.completion_rate)} | {states} |")
    return "\n".join(lines)


def category_table(m: ModelReport) -> str:
    lines = ["| Category | Weight | Score |", "|---|---:|---:|"]
    for cat in sorted(m.category_scores):
        lines.append(f"| {CATEGORY_TITLES.get(cat, cat)} ({cat}) | "
                     f"{CATEGORY_WEIGHTS.get(cat, 1.0)} | "
                     f"{_num(m.category_scores[cat] * 100, 0)} |")
    return "\n".join(lines)


def render_markdown(report: RunReport) -> str:
    out: list[str] = [
        "# Aurora Intelligence Lab — report",
        "",
        f"Generated: {report.generated_at}",
        "",
        "Settings: " + ", ".join(f"{k}={v}" for k, v in report.settings.items()
                                 if k not in ("note", "persona", "categories")),
        "",
        report.settings.get("note", ""),
        "",
        "## Ranking (intelligence first)",
        "",
        ranking_table(report),
        "",
        "Intelligence = weighted mean of category scores over scored trials "
        "(empty/reasoning-only answers score 0; integration errors excluded). "
        "Completion and speed are reported separately by design.",
        "",
    ]
    for m in report.models:
        out += [
            f"## {m.model}",
            "",
            f"- Backend: `{m.backend}` · intelligence score: "
            f"**{_num(m.intelligence_score * 100, 1)}** · visible completion: "
            f"{_pct(m.visible_completion_rate)} · tool success: "
            f"{_pct(m.tool_success_rate)}",
            f"- Context — {context_table(m)}",
            f"- Speed (diagnostics, not scored) — {speed_table(m)}",
            "",
            "### Category scores",
            "",
            category_table(m),
            "",
            "### Scenario detail",
            "",
            scenario_table(m),
            "",
        ]
        empty_runs = [(t.scenario_id, t.trial_no, t.state)
                      for sid, s in m.scenario_results.items()
                      for t in s.trials
                      if t.state in (STATE_EMPTY, STATE_EMPTY_WITH_THINKING)]
        if empty_runs:
            out += ["### Empty / reasoning-only answers",
                    ""]
            out += [f"- `{sid}` trial {n}: **{state}** — counted as a failure, "
                    "never as a pass." for sid, n, state in empty_runs]
            out += [""]
        if m.integration_errors:
            out += ["### Integration / transport issues",
                    "",
                    "Excluded from scoring — provider problems, not model quality:",
                    ""]
            out += [f"- {e}" for e in m.integration_errors]
            out += [""]
    out += ["## Methodology",
            "",
            "Scoring is rubric-based and objective wherever possible (exact "
            "facts, numbers, tool calls, correction handling, uncertainty "
            "markers, repetition ratios). Tone and humor checks are heuristic "
            "and flagged in the scenario notes — no model is used as a judge "
            "of another model. Multi-turn scenarios re-send the full visible "
            "history each turn. Each server's effective context is read via "
            "its API and never modified; long-context scenarios are skipped "
            "honestly for models whose effective limit cannot hold them.",
            ""]
    return "\n".join(out)


def export_report(report: RunReport, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "report.md"
    json_path = out_dir / "report.json"
    md_path.write_text(render_markdown(report), encoding="utf-8")
    json_path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False),
                         encoding="utf-8")
    return {"markdown": str(md_path), "json": str(json_path)}
