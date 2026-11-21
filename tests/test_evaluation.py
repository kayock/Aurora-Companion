"""Intelligence Lab tests: fair accounting (empty responses never pass),
thinking-channel separation, integration-error isolation, tool scoring,
context inspection, multi-turn history, report export — all offline via
mock/scripted backends. The harness must be DB-free: no aurora.db anywhere.
"""

from __future__ import annotations

import json

import httpx
import pytest

from aurora.evaluation import (
    Completion, IntelligenceLab, IntegrationError, MockEvalBackend,
    OllamaBackend, OpenAICompatBackend, STATE_EMPTY,
    STATE_EMPTY_WITH_THINKING, STATE_INTEGRATION_ERROR,
    build_scenarios, export_report,
)
from aurora.evaluation.backends import split_thinking
from aurora.evaluation.models import Check, Scenario, ScenarioTurn
from aurora.evaluation.scoring import grade_check, grade_turn


# ---------------------------------------------------------------- thinking channels
def test_split_thinking_inline_blocks():
    visible, thinking = split_thinking(
        "<think>let me add: 14:20 + 80min</think>The train arrives at 15:40.")
    assert visible == "The train arrives at 15:40."
    assert "80min" in thinking and "15:40" in visible


def test_split_thinking_unterminated():
    visible, thinking = split_thinking("<think>reasoning forever, no answer")
    assert visible == "" and "reasoning forever" in thinking


def test_ollama_backend_parses_message_thinking_and_timing():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        body = json.loads(request.content)
        assert body["model"] == "deepseek-q8:latest"
        return httpx.Response(200, json={
            "message": {"role": "assistant", "content": "The answer is 42.",
                        "thinking": "step one... step two..."},
            "total_duration": 5_000_000_000,
            "prompt_eval_duration": 1_000_000_000,
            "eval_duration": 2_000_000_000,
            "eval_count": 60, "prompt_eval_count": 300,
        })

    backend = OllamaBackend("deepseek-q8:latest",
                            transport=httpx.MockTransport(handler))
    completion = asyncio_run(backend.complete([{"role": "user", "content": "q"}]))
    assert completion.visible == "The answer is 42."
    assert completion.thinking == "step one... step two..."
    assert completion.state == "completed"
    assert completion.timing["prompt_eval_s"] == 1.0
    assert completion.timing["generation_s"] == 2.0
    assert completion.timing["prompt_tokens"] == 300
    assert completion.timing["output_tokens"] == 60
    assert completion.timing["tokens_per_s"] == 30.0


def test_ollama_backend_tool_calls_and_http_error_is_integration():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/chat":
            return httpx.Response(200, json={"message": {
                "content": "",
                "tool_calls": [{"function": {"name": "get_weather",
                                             "arguments": {"city": "Oslo"}}}]}})
        return httpx.Response(500)

    backend = OllamaBackend("m", transport=httpx.MockTransport(handler))
    c = asyncio_run(backend.complete([{"role": "user", "content": "weather?"}],
                                     tools=[{"type": "function"}]))
    assert c.tool_calls and c.tool_calls[0]["name"] == "get_weather"
    assert json.loads(c.tool_calls[0]["arguments"])["city"] == "Oslo"
    down = OllamaBackend("m", transport=httpx.MockTransport(
        lambda r: httpx.Response(503)))
    with pytest.raises(IntegrationError):
        asyncio_run(down.complete([{"role": "user", "content": "q"}]))


def test_openai_compat_reasoning_content_and_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(200, json={"choices": [{
            "message": {"role": "assistant",
                        "content": "<think>hidden</think>Visible answer.",
                        "reasoning_content": "server-side reasoning"},
            "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 20}})

    backend = OpenAICompatBackend("local-model", "http://127.0.0.1:1234/v1",
                                  transport=httpx.MockTransport(handler))
    c = asyncio_run(backend.complete([{"role": "user", "content": "q"}]))
    assert c.visible == "Visible answer."
    # reasoning_content wins over inline <think> (both captured)
    assert "server-side reasoning" in c.thinking
    assert "hidden" in c.thinking
    assert c.timing["output_tokens"] == 20

    err = OpenAICompatBackend("local-model", "http://127.0.0.1:1234/v1",
                              transport=httpx.MockTransport(
                                  lambda r: httpx.Response(404)))
    with pytest.raises(IntegrationError):
        asyncio_run(err.complete([{"role": "user", "content": "q"}]))


def test_ollama_inspect_effective_context():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/show":
            return httpx.Response(200, json={
                "model_info": {"general.architecture": "llama",
                               "qwen2.context_length": 131072,
                               "llama.context_length": 32768},
                "parameters": "num_ctx 8192\nstop \"<|eot|>\""})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{
                "name": "llama3.2:3b", "size_vram": 2_800_000_000,
                "context_length": 8192}]})
        return httpx.Response(404)

    backend = OllamaBackend("llama3.2:3b", transport=httpx.MockTransport(handler))
    info = asyncio_run(backend.inspect())
    assert info["advertised_context_lengths"] == [32768, 131072]
    assert info["num_ctx_parameter"] == 8192
    assert info["effective_context"] == 8192   # recorded, never modified
    assert info["loaded_size_bytes"] == 2_800_000_000


# ---------------------------------------------------------------- scoring engine
def _one(kind, **params):
    return [Check(kind=kind, params=params)]


def test_grader_basics():
    ok, _ = grade_turn(_one("contains_all", words=["marta", "oslo"]),
                       "Marta lives in Oslo.", [], [], {})
    assert ok == 1.0
    miss, _ = grade_turn(_one("contains_all", words=["marta", "oslo"]),
                         "Marta lives in Bergen.", [], [], {})
    assert miss == 0.0
    bad, _ = grade_turn(_one("not_contains", words=["oslo"]),
                        "Bergen, of course.", [], [], {})
    assert bad == 1.0


def test_grader_number_and_uncertainty():
    ok, _ = grade_turn(_one("answer_number", accepted=["15:40", "3:40"]),
                       "It arrives at 15:40.", [], [], {})
    assert ok == 1.0
    ok, _ = grade_turn(_one("uncertainty_expressed"),
                       "I have no way to know that, Alex.", [], [], {})
    assert ok == 1.0
    fail, _ = grade_turn(_one("uncertainty_expressed"),
                         "You had pancakes!", [], [], {})
    assert fail == 0.0


def test_grader_no_repetition_uses_prior_replies():
    prior = ["Coffee sounds great today."]
    same, _ = grade_turn(_one("no_repetition", max_similarity=0.75),
                         "Coffee sounds great today!", prior, [], {})
    assert same == 0.0
    fresh, _ = grade_turn(_one("no_repetition", max_similarity=0.75),
                          "A coarser grind slows extraction.", prior, [], {})
    assert fresh == 1.0


def test_grader_tool_checks():
    call = {"name": "get_weather", "arguments": json.dumps({"city": "Oslo"})}
    ok, _ = grade_turn(_one("tool_called", tool="get_weather"),
                       "It's snowing in Oslo.", [], [call], {})
    assert ok == 1.0
    wrong, _ = grade_turn(_one("tool_called", tool="get_weather"),
                          "It's snowing.", [], [{"name": "remember_note",
                                                 "arguments": "{}"}], {})
    assert wrong == 0.0
    missing_args, _ = grade_turn(_one("tool_args_valid", tool="get_weather",
                                      required=["city"]),
                                 "Sure.", [], [{"name": "get_weather",
                                                "arguments": "{}"}], {})
    assert missing_args == 0.0
    used, _ = grade_turn(_one("tool_result_used", marker="snow"),
                         "Light snow, 3°C.", [], [call],
                         {"get_weather": '{"sky": "light snow"}'})
    assert used == 1.0


def test_grader_empty_visible_fails_everything():
    score, results = grade_turn(_one("contains_any", words=["yes"]),
                                "", [], [], {})
    assert score == 0.0 and results[0].passed is False


# ---------------------------------------------------------------- harness fairness
def _lab(handler, scenarios=None, trials=2):
    factory = lambda m: MockEvalBackend(m, handler=handler)
    return IntelligenceLab(factory, scenarios=scenarios, trials=trials)


def _scen(id, turns, tools=None):
    return Scenario(id=id, category="reasoning", title=id, description=id,
                    turns=turns, tools=tools)


async def _run(async_fn):
    return await async_fn


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


def test_empty_visible_never_passes():
    """The Part-3 core rule: empty (or thinking-only) answers score 0 and
    count against completion — they are never passes."""
    calls = {"n": 0}

    def handler(sid, turn_no, trial, messages, tools):
        calls["n"] += 1
        if trial == 2:
            return Completion(visible="", thinking="I reasoned but said nothing.",
                              timing={"wall_s": 1.0})
        return Completion(visible="Grieg is your favorite composer, Alex.",
                          timing={"wall_s": 1.0, "tokens_per_s": 50.0})

    scen = _scen("s1", [ScenarioTurn(user="Who is my favorite composer?",
                                     checks=_one("contains_all", words=["grieg"]))])
    lab = _lab(handler, [scen], trials=2)
    report = asyncio_run(_run(lab.run(["m1"])))
    m = report.models[0]
    sres = m.scenario_results["s1"]
    states = {t.trial_no: t.state for t in sres.trials}
    assert states[1] == "completed" and states[2] == STATE_EMPTY_WITH_THINKING
    assert sres.trials[1].score == 0.0          # empty → 0, never a pass
    assert sres.trials[0].score == 1.0
    assert sres.mean_score == 0.5               # punished, visible in report
    assert sres.completion_rate == 0.5
    assert m.visible_completion_rate == 0.5
    # thinking was still captured for inspection
    assert "reasoned" in sres.trials[1].turns[0].thinking


def test_integration_errors_are_separated_from_model_quality():
    def handler(sid, turn_no, trial, messages, tools):
        if trial == 2:
            raise IntegrationError("connection refused")
        return Completion(visible="A fine answer, Alex.", timing={"wall_s": 1.0})

    scen = _scen("s1", [ScenarioTurn(user="hi",
                                     checks=_one("min_len", chars=5))])
    lab = _lab(handler, [scen], trials=2)
    report = asyncio_run(_run(lab.run(["m1"])))
    m = report.models[0]
    sres = m.scenario_results["s1"]
    assert sres.n_integration_errors == 1
    assert sres.completion_rate == 1.0            # computed over scoring trials only
    assert sres.mean_score == 1.0                 # transport failure ≠ model failure
    assert m.integration_errors and "s1" in m.integration_errors[0]
    assert m.visible_completion_rate == 1.0


def test_multiturn_history_grows_and_recall_passes():
    seen_lengths: list[int] = []

    def handler(sid, turn_no, trial, messages, tools):
        seen_lengths.append(len(messages))
        if turn_no == 2:
            return Completion(visible="Your sister is Marta and she lives in "
                                      "Oslo, Alex.", timing={"wall_s": 0.5})
        return Completion(visible="Noted, Alex — thanks for telling me.",
                          timing={"wall_s": 0.5})

    scen = _scen("mem", [
        ScenarioTurn(user="My sister Marta moved to Oslo."),
        ScenarioTurn(user="Cool weather lately."),
        ScenarioTurn(user="What's my sister's name and where does she live?",
                     checks=_one("contains_all", words=["marta", "oslo"])),
    ])
    lab = _lab(handler, [scen], trials=1)
    report = asyncio_run(_run(lab.run(["m1"])))
    # history grows: system + prior pairs + current user each turn
    assert seen_lengths == [2, 4, 6]
    t = report.models[0].scenario_results["mem"].trials[0]
    assert t.score == 1.0


def test_tool_flow_scripted_call_result_and_usage():
    def handler(sid, turn_no, trial, messages, tools):
        last = messages[-1]
        if last.get("role") == "tool":
            # the tool result reached the model — it must inform the reply
            assert "light snow" in last["content"]
            return Completion(visible="It's light snow and 3°C in Oslo, Alex.",
                              timing={"wall_s": 0.5})
        return Completion(visible="", tool_calls=[{
            "name": "get_weather", "arguments": json.dumps({"city": "Oslo"})}],
            timing={"wall_s": 0.5})

    scen = _scen("tool", [ScenarioTurn(
        user="Weather in Oslo?",
        tool_results={"get_weather": '{"sky": "light snow", "temp": "3°C"}'},
        checks=[Check(kind="tool_called", params={"tool": "get_weather"}),
                Check(kind="tool_args_valid", params={"tool": "get_weather",
                                                      "required": ["city"]}),
                Check(kind="tool_result_used", params={"marker": "snow"})])],
        tools=[{"type": "function",
                "function": {"name": "get_weather", "description": "weather",
                             "parameters": {"type": "object", "properties": {}}}}])
    lab = _lab(handler, [scen], trials=1)
    report = asyncio_run(_run(lab.run(["m1"])))
    m = report.models[0]
    t = m.scenario_results["tool"].trials[0]
    assert t.state == "completed" and t.score == 1.0
    assert t.turns[0].tool_calls[0]["name"] == "get_weather"
    assert m.tool_success_rate == 1.0


def test_unknown_tool_and_malformed_result_flow():
    def handler(sid, turn_no, trial, messages, tools):
        last = messages[-1]
        if last.get("role") == "tool":
            return Completion(visible="The weather service gave me a broken "
                                      "response — I couldn't read Oslo's "
                                      "weather. Want me to try again?",
                              timing={"wall_s": 0.5})
        return Completion(visible="", tool_calls=[{
            "name": "get_weather", "arguments": "{}"}], timing={"wall_s": 0.5})

    scen = _scen("broken", [ScenarioTurn(
        user="Weather?",
        tool_results={"get_weather": '{"broken'},
        checks=[Check(kind="contains_any",
                      params={"words": ["couldn't", "error", "broken"]})])],
        tools=[{"type": "function",
                "function": {"name": "get_weather", "description": "w",
                             "parameters": {"type": "object", "properties": {}}}}])
    lab = _lab(handler, [scen], trials=1)
    report = asyncio_run(_run(lab.run(["m1"])))
    t = report.models[0].scenario_results["broken"].trials[0]
    assert t.score == 1.0
    assert '{"broken' in t.turns[0].tool_results["get_weather"]


def test_identical_settings_across_models():
    seen = []

    def make_handler(tag):
        def handler(sid, turn_no, trial, messages, tools):
            seen.append((messages.eval_meta.get("scenario_id"), tag))
            return Completion(visible="ok, Alex", timing={"wall_s": 0.1})
        return handler

    backends = {}
    def factory(model):
        backend = MockEvalBackend(model, handler=make_handler(model))
        backends[model] = backend
        return backend

    scen = _scen("s", [ScenarioTurn(user="hi", checks=[])])
    lab = IntelligenceLab(factory, scenarios=[scen], trials=1,
                          temperature=0.4, max_tokens=777)
    asyncio_run(_run(lab.run(["a", "b"])))
    temps = {(b.calls[0]["temperature"], b.calls[0]["max_tokens"])
             for b in backends.values()}
    assert temps == {(0.4, 777)}          # identical settings for every model


def test_long_context_scenario_skipped_when_over_effective_limit():
    def handler(sid, turn_no, trial, messages, tools):
        return Completion(visible="fine, Alex", timing={"wall_s": 0.1})

    big = _scen("big", [ScenarioTurn(user="word " * 20000,
                                     checks=_one("min_len", chars=1))])
    lab = _lab(handler, [big], trials=1)
    lab.backend_factory = lambda m: MockEvalBackend(
        m, handler=handler,
        context_info={"effective_context": 1024})   # tiny effective ctx
    report = asyncio_run(_run(lab.run(["m1"])))
    m = report.models[0]
    assert m.scenario_results["big"].trials == []
    assert any("skipped" in e and "effective" in e
               for e in m.integration_errors)
    assert m.intelligence_score == 0.0 and m.visible_completion_rate == 0.0


def test_ranking_orders_by_intelligence_not_speed():
    from aurora.evaluation.models import ModelReport, RunReport
    fast_dumb = ModelReport(model="fast-dumb", backend="mock",
                            intelligence_score=0.40,
                            visible_completion_rate=1.0,
                            speed={"mean_tokens_per_s": 300.0})
    slow_smart = ModelReport(model="slow-smart", backend="mock",
                             intelligence_score=0.85,
                             visible_completion_rate=1.0,
                             speed={"mean_tokens_per_s": 12.0})
    report = RunReport(generated_at="t", settings={}, models=[fast_dumb, slow_smart])
    report.ranking = IntelligenceLab._rank(report.models)
    assert report.ranking[0]["model"] == "slow-smart"
    assert report.ranking[0]["rank"] == 1


# ---------------------------------------------------------------- catalog + report
def test_scenario_catalog_covers_all_seven_categories():
    scenarios = build_scenarios(context_turns=10)
    cats = {s.category for s in scenarios}
    assert cats == {"conversational", "reasoning", "memory", "personality",
                    "tool", "context", "recovery"}
    ids = [s.id for s in scenarios]
    assert len(ids) == len(set(ids)) and len(scenarios) >= 24
    for s in scenarios:
        assert s.turns and all(t.user.strip() for t in s.turns)
        assert s.turns[-1].checks, "every scenario ends with graded checks"


def test_context_scenarios_scale_with_filler():
    short = build_scenarios(context_turns=8)
    long = build_scenarios(context_turns=30)
    def ctx_len(scenarios):
        s = next(x for x in scenarios if x.id == "ctx_long_retrieval")
        return s.n_turns
    assert ctx_len(long) > ctx_len(short) >= 9


def test_report_export_markdown_and_json(tmp_path):
    def handler(sid, turn_no, trial, messages, tools):
        if trial == 2:
            return Completion(visible="", thinking="hmm", timing={"wall_s": 1.0})
        return Completion(visible="A good answer, Alex.", timing={"wall_s": 1.0,
                                                                  "tokens_per_s": 40.0})
    scen = _scen("s1", [ScenarioTurn(user="q", checks=_one("min_len", chars=5))])
    lab = _lab(handler, [scen], trials=2)
    report = asyncio_run(_run(lab.run(["m1", "m2"])))
    paths = export_report(report, tmp_path / "out")
    md = (tmp_path / "out" / "report.md").read_text()
    data = json.loads((tmp_path / "out" / "report.json").read_text())
    assert "Intelligence Lab" in md and "Ranking" in md
    assert "empty_with_thinking" in md          # failure modes are visible
    assert data["ranking"][0]["model"] == "m1" or \
        data["ranking"][1]["model"] == "m1"
    assert data["models"][0]["scenario_results"]["s1"]["trials"][1]["state"] == \
        STATE_EMPTY_WITH_THINKING


def test_harness_is_db_free(tmp_path):
    """The Intelligence Lab must never create or touch an Aurora database."""
    def handler(sid, turn_no, trial, messages, tools):
        return Completion(visible="ok, Alex", timing={"wall_s": 0.1})
    scen = _scen("s", [ScenarioTurn(user="hi", checks=[])])
    lab = _lab(handler, [scen], trials=1)
    asyncio_run(_run(lab.run(["m1"])))
    dbs = list(tmp_path.rglob("aurora.db")) + list(tmp_path.rglob("*.db"))
    assert dbs == []
