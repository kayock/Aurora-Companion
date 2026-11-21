"""The Intelligence Lab runner.

Orchestrates model × scenario × trial evaluation with these fairness rules:

* identical prompts (same persona, same scenario scripts) and identical
  generation settings (temperature/max_tokens) for every model in a run;
* repeated trials per scenario (default 3);
* full multi-turn history is re-sent each turn (visible text only — thinking
  channels are recorded but never re-fed, mirroring production behavior);
* a trial/turn with an empty visible answer can NEVER pass: it scores 0 and
  counts against the visible completion rate. Thinking-only answers
  (DeepSeek-style) are recorded as their own state so the failure mode is
  visible, not silently folded into "wrong answer";
* endpoint/transport failures are IntegrationErrors — excluded from scoring
  entirely and reported in the diagnostics, so provider problems are never
  mistaken for model stupidity;
* each server's EFFECTIVE context limit is read via the backend and long-
  context scenarios are skipped (honestly) for models whose limit cannot
  hold them — the harness never widens a context window;
* nothing in this module opens a database or touches Aurora's stores: the
  harness is self-contained and cannot disturb production history, memories
  or persona.

Speed metrics are collected and reported but are NOT part of the
intelligence score.
"""

from __future__ import annotations

import asyncio
import datetime as _dt

from .backends import EvalMessages, IntegrationError
from .models import (
    STATE_COMPLETED, STATE_EMPTY, STATE_EMPTY_WITH_THINKING,
    STATE_INTEGRATION_ERROR, ModelReport, RunReport, ScenarioResult,
    TrialResult, TurnResult,
)
from .scenarios import CATEGORY_WEIGHTS, EVAL_PERSONA, Scenario
from .scoring import grade_turn


class IntelligenceLab:
    def __init__(self, backend_factory, scenarios: list[Scenario] | None = None,
                 trials: int = 3, temperature: float = 0.7,
                 max_tokens: int = 1024, persona: str = EVAL_PERSONA,
                 filler_turns: int = 14) -> None:
        self.backend_factory = backend_factory
        self.trials = max(1, int(trials))
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.persona = persona
        if scenarios is None:
            from .scenarios import build_scenarios
            scenarios = build_scenarios(context_turns=filler_turns)
        self.scenarios = scenarios

    # ---------------------------------------------------------------- run
    async def run(self, models: list[str]) -> RunReport:
        report = RunReport(
            generated_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            settings={
                "trials": self.trials,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "scenarios": len(self.scenarios),
                "categories": sorted(CATEGORY_WEIGHTS),
                "persona": "fixed EVAL_PERSONA (isolated from production)",
                "note": "intelligence is the primary score; speed is reported "
                        "separately and never blended into it",
            },
        )
        for model in models:
            backend = self.backend_factory(model)
            try:
                ctx = await backend.inspect()
            except IntegrationError as exc:
                ctx = {"error": str(exc)}
            except Exception as exc:  # inspect must never kill the run
                ctx = {"error": f"inspect failed: {exc}"}
            mrep = await self._eval_model(model, backend, ctx)
            report.models.append(mrep)
            await backend.close()
        report.ranking = self._rank(report.models)
        return report

    # ---------------------------------------------------------------- per model
    async def _eval_model(self, model: str, backend, ctx: dict) -> ModelReport:
        mrep = ModelReport(model=model, backend=getattr(backend, "name", "base"),
                           context_info=ctx)
        eff_ctx = ctx.get("effective_context")
        for scenario in self.scenarios:
            est = self._estimate_tokens(scenario)
            if isinstance(eff_ctx, int) and est > eff_ctx * 0.9:
                # honest skip: the transcript does not fit the model's
                # effective window; the harness never widens it
                mrep.scenario_results[scenario.id] = ScenarioResult(
                    scenario_id=scenario.id, category=scenario.category)
                mrep.integration_errors.append(
                    f"{scenario.id}: skipped — needs ~{est} tokens, effective "
                    f"context {eff_ctx}")
                continue
            sres = ScenarioResult(scenario_id=scenario.id, category=scenario.category)
            for trial_no in range(1, self.trials + 1):
                trial = await self._run_trial(scenario, trial_no, backend)
                sres.trials.append(trial)
            self._aggregate_scenario(sres)
            mrep.scenario_results[scenario.id] = sres
        self._finalize_model_report(mrep)
        return mrep

    async def _run_trial(self, scenario: Scenario, trial_no: int, backend) -> TrialResult:
        trial = TrialResult(scenario_id=scenario.id, trial_no=trial_no,
                            state=STATE_COMPLETED)
        history: list[dict] = [{"role": "system", "content": self.persona}]
        prior_replies: list[str] = []
        worst_state = STATE_COMPLETED
        import time as _time
        t_wall0 = _time.perf_counter()
        for turn_no, turn in enumerate(scenario.turns):
            msgs = EvalMessages(history + [{"role": "user", "content": turn.user}],
                                {"scenario_id": scenario.id, "turn_no": turn_no,
                                 "trial_no": trial_no})
            visible, thinking = "", ""
            tool_calls_made: list[dict] = []
            executed: dict[str, tuple[str, str]] = {}
            executed_by_name: dict[str, str] = {}
            timing: dict = {}
            error = ""
            turn_state = STATE_COMPLETED
            rounds = 0
            while True:
                try:
                    completion = await backend.complete(
                        msgs, tools=scenario.tools,
                        temperature=self.temperature, max_tokens=self.max_tokens)
                except IntegrationError as exc:
                    turn_state = STATE_INTEGRATION_ERROR
                    error = str(exc)
                    break
                except Exception as exc:  # defensive: harness bug, not model
                    turn_state = STATE_INTEGRATION_ERROR
                    error = f"harness error: {exc}"
                    break
                timing = dict(completion.timing)
                thinking = completion.thinking or thinking
                if completion.tool_calls and scenario.tools \
                        and rounds < scenario.max_tool_rounds:
                    msgs = self._tool_round_messages(msgs, completion)
                    for i, call in enumerate(completion.tool_calls):
                        name = str(call.get("name", ""))
                        tool_calls_made.append(call)
                        result = self._execute_tool(name, call, turn.tool_results)
                        executed[f"call_{i}"] = (name, result)
                        executed_by_name[name] = result
                    msgs = self._append_tool_results(msgs, executed)
                    rounds += 1
                    continue
                visible = (completion.visible or "").strip()
                turn_state = completion.state
                break
            if turn_state == STATE_COMPLETED and not visible and thinking:
                turn_state = STATE_EMPTY_WITH_THINKING
            if turn_state == STATE_COMPLETED and not visible and not thinking:
                turn_state = STATE_EMPTY
            score, checks = 0.0, []
            if turn_state != STATE_INTEGRATION_ERROR:
                score, checks = grade_turn(turn.checks, visible, prior_replies,
                                           tool_calls_made, executed_by_name)
            prior_replies.append(visible)
            history.append({"role": "user", "content": turn.user})
            history.append({"role": "assistant", "content": visible})
            trial.turns.append(TurnResult(
                user=turn.user, visible=visible, thinking=thinking,
                state=turn_state, checks=checks, score=score,
                tool_calls=tool_calls_made, tool_results=executed_by_name,
                timing=timing))
            if turn_state == STATE_INTEGRATION_ERROR:
                worst_state = STATE_INTEGRATION_ERROR
                trial.error = error
            elif worst_state != STATE_INTEGRATION_ERROR and turn_state != STATE_COMPLETED:
                worst_state = turn_state
        trial.wall_seconds = round(_time.perf_counter() - t_wall0, 3)
        trial.state = worst_state
        if worst_state == STATE_INTEGRATION_ERROR:
            trial.score = 0.0
        else:
            scored = [t.score for t in trial.turns]
            trial.score = round(sum(scored) / len(scored), 4) if scored else 0.0
        trial.timing = self._merge_timing(trial.turns)
        return trial

    @staticmethod
    def _execute_tool(name: str, call: dict, scripted: dict[str, str]) -> str:
        """Scripted tool execution. Unknown tools error; malformed scripted
        payloads are passed through verbatim — that IS the scenario."""
        if name in scripted:
            return scripted[name]
        return f"error: unknown tool {name}"

    @staticmethod
    def _tool_round_messages(msgs: list[dict], completion) -> list[dict]:
        """OpenAI-style follow-up so both backends can continue after calls."""
        out = list(msgs)
        out.append({
            "role": "assistant", "content": completion.visible or "",
            "tool_calls": [
                {"id": f"call_{i}", "type": "function",
                 "function": {"name": c.get("name", ""),
                              "arguments": c.get("arguments") or "{}"}}
                for i, c in enumerate(completion.tool_calls)
            ],
        })
        return out

    @staticmethod
    def _append_tool_results(msgs: list[dict],
                             results: dict[str, tuple[str, str]]) -> list[dict]:
        """Append role=tool messages after the assistant tool_calls message.
        `results` maps call_id → (tool_name, result_text)."""
        out = list(msgs)
        for call_id, (name, result) in results.items():
            out.append({"role": "tool", "tool_call_id": call_id,
                        "content": result})
        return out

    # ---------------------------------------------------------------- aggregation
    @staticmethod
    def _estimate_tokens(scenario: Scenario) -> int:
        chars = len(EVAL_PERSONA)
        for turn in scenario.turns:
            chars += len(turn.user) + 80  # +80: expected assistant reply overhead
        return chars // 4 + 64

    @staticmethod
    def _aggregate_scenario(sres: ScenarioResult) -> None:
        scoring = [t for t in sres.trials if t.counts_for_score]
        completed = [t for t in scoring if t.state == STATE_COMPLETED]
        sres.n_completed = len(completed)
        sres.n_integration_errors = len(sres.trials) - len(scoring)
        if scoring:
            sres.mean_score = round(sum(t.score for t in scoring) / len(scoring), 4)
            sres.completion_rate = round(len(completed) / len(scoring), 4)

    @staticmethod
    def _merge_timing(turns: list[TurnResult]) -> dict:
        agg: dict[str, float] = {}
        for t in turns:
            for k, v in (t.timing or {}).items():
                if isinstance(v, (int, float)):
                    agg[k] = agg.get(k, 0.0) + float(v)
        n = max(1, len(turns))
        return {k: round(v / n, 3) for k, v in agg.items()}

    @staticmethod
    def _finalize_model_report(mrep: ModelReport) -> None:
        # category means over scenario mean scores
        per_cat: dict[str, list[float]] = {}
        all_scoring_trials: list[TrialResult] = []
        tool_checks: list[tuple[bool, bool]] = []
        for sres in mrep.scenario_results.values():
            if sres.mean_score or sres.trials:
                per_cat.setdefault(sres.category, []).append(sres.mean_score)
            all_scoring_trials.extend(t for t in sres.trials if t.counts_for_score)
            for t in sres.trials:
                if t.state == STATE_INTEGRATION_ERROR:
                    continue
                for turn in t.turns:
                    called_ok = [c for c in turn.checks if c.kind == "tool_called"]
                    args_ok = [c for c in turn.checks if c.kind == "tool_args_valid"]
                    for c in called_ok:
                        tool_checks.append((c.passed, True))
                    for c in args_ok:
                        tool_checks.append((c.passed, True))
        for cat, scores in per_cat.items():
            mrep.category_scores[cat] = round(sum(scores) / len(scores), 4) \
                if scores else 0.0
        weight_total = sum(CATEGORY_WEIGHTS.get(c, 1.0)
                           for c in mrep.category_scores)
        if weight_total:
            mrep.intelligence_score = round(
                sum(mrep.category_scores[c] * CATEGORY_WEIGHTS.get(c, 1.0)
                    for c in mrep.category_scores) / weight_total, 4)
        scoring_trials = [t for sres in mrep.scenario_results.values()
                          for t in sres.trials if t.counts_for_score]
        if scoring_trials:
            mrep.visible_completion_rate = round(
                sum(1 for t in scoring_trials if t.state == STATE_COMPLETED)
                / len(scoring_trials), 4)
        if tool_checks:
            mrep.tool_success_rate = round(
                sum(1 for ok, _ in tool_checks if ok) / len(tool_checks), 4)
        walls = [t.wall_seconds for t in all_scoring_trials if t.wall_seconds]
        tps = [t.timing.get("tokens_per_s") for t in all_scoring_trials
               if t.timing.get("tokens_per_s")]
        ttft = [t.timing.get("ttft_s") for t in all_scoring_trials
                if t.timing.get("ttft_s") is not None]
        peval = [t.timing.get("prompt_eval_s") for t in all_scoring_trials
                 if t.timing.get("prompt_eval_s") is not None]
        gen = [t.timing.get("generation_s") for t in all_scoring_trials
               if t.timing.get("generation_s") is not None]
        mrep.speed = {
            "mean_wall_s": round(sum(walls) / len(walls), 3) if walls else None,
            "mean_tokens_per_s": round(sum(tps) / len(tps), 2) if tps else None,
            "mean_ttft_s": round(sum(ttft) / len(ttft), 3) if ttft else None,
            "mean_prompt_eval_s": round(sum(peval) / len(peval), 3) if peval else None,
            "mean_generation_s": round(sum(gen) / len(gen), 3) if gen else None,
        }
        mrep.integration_errors = [e for e in mrep.integration_errors] or [
            f"{sid}: {sres.n_integration_errors} integration error trial(s)"
            for sid, sres in mrep.scenario_results.items()
            if sres.n_integration_errors]

    # ---------------------------------------------------------------- ranking
    @staticmethod
    def _rank(models: list[ModelReport]) -> list[dict]:
        ordered = sorted(models, key=lambda m: (m.intelligence_score,
                                                m.visible_completion_rate),
                         reverse=True)
        return [{
            "rank": i + 1,
            "model": m.model,
            "intelligence_score": m.intelligence_score,
            "visible_completion_rate": m.visible_completion_rate,
            "tool_success_rate": m.tool_success_rate,
            "mean_tokens_per_s": m.speed.get("mean_tokens_per_s"),
            "mean_wall_s": m.speed.get("mean_wall_s"),
            "effective_context": m.context_info.get("effective_context"),
        } for i, m in enumerate(ordered)]
