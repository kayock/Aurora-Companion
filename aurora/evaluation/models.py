"""Data model for the Intelligence Lab (aurora.evaluation).

Small dataclasses only — the evaluation harness is deliberately DB-free so
it can never touch Aurora's production conversation history, memories, or
persona. Reports are plain dicts/JSON.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Trial lifecycle states. The Part-3 fairness rules are encoded here:
#   completed            — visible answer produced (only this can score > 0)
#   empty_with_thinking  — model produced reasoning but NO visible answer
#                          (the observed DeepSeek Q8 failure mode; scores 0)
#   empty_visible        — nothing at all; scores 0
#   integration_error    — endpoint/transport failure; NOT a model-quality
#                          failure, excluded from scoring, shown in diagnostics
STATE_COMPLETED = "completed"
STATE_EMPTY_WITH_THINKING = "empty_with_thinking"
STATE_EMPTY = "empty_visible"
STATE_INTEGRATION_ERROR = "integration_error"

SCORING_STATES = (STATE_COMPLETED, STATE_EMPTY_WITH_THINKING, STATE_EMPTY)


@dataclass
class Check:
    """One objective scoring criterion for a turn."""
    kind: str
    params: dict = field(default_factory=dict)
    note: str = ""  # human-readable description shown in reports


@dataclass
class CheckResult:
    kind: str
    note: str
    passed: bool
    observed: str = ""


@dataclass
class ScenarioTurn:
    user: str
    checks: list[Check] = field(default_factory=list)
    # scripted tool results for this turn: tool name → JSON string result
    tool_results: dict[str, str] = field(default_factory=dict)


@dataclass
class Scenario:
    id: str
    category: str
    title: str
    description: str
    turns: list[ScenarioTurn]
    tools: list[dict] | None = None      # OpenAI-style tool specs offered to the model
    max_tool_rounds: int = 2
    grader_note: str = ""                # objectivity caveat printed in the report

    @property
    def n_turns(self) -> int:
        return len(self.turns)


@dataclass
class Completion:
    """One model completion, normalized across backends."""
    visible: str = ""
    thinking: str = ""
    tool_calls: list[dict] = field(default_factory=list)  # {name, arguments(json str)}
    timing: dict = field(default_factory=dict)  # seconds / tokens (see metrics)
    error: str | None = None
    raw_state: str = STATE_COMPLETED

    @property
    def state(self) -> str:
        if self.error:
            return STATE_INTEGRATION_ERROR
        if self.visible.strip():
            return STATE_COMPLETED
        if self.thinking.strip():
            return STATE_EMPTY_WITH_THINKING
        return STATE_EMPTY


@dataclass
class TurnResult:
    user: str
    visible: str
    thinking: str
    state: str
    checks: list[CheckResult] = field(default_factory=list)
    score: float = 0.0                    # fraction of applicable checks passed
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: dict[str, str] = field(default_factory=dict)
    timing: dict = field(default_factory=dict)


@dataclass
class TrialResult:
    scenario_id: str
    trial_no: int
    state: str                            # worst turn state decides
    turns: list[TurnResult] = field(default_factory=list)
    score: float = 0.0                    # mean of turn scores (0 when not completed)
    error: str = ""
    wall_seconds: float = 0.0
    timing: dict = field(default_factory=dict)

    @property
    def counts_for_score(self) -> bool:
        return self.state != STATE_INTEGRATION_ERROR


@dataclass
class ScenarioResult:
    scenario_id: str
    category: str
    trials: list[TrialResult] = field(default_factory=list)
    mean_score: float = 0.0               # over scoring trials (completed+empty)
    completion_rate: float = 0.0          # completed / (completed+empty+empty_thinking)
    n_integration_errors: int = 0
    n_completed: int = 0


@dataclass
class ModelReport:
    model: str
    backend: str
    context_info: dict = field(default_factory=dict)
    scenario_results: dict[str, ScenarioResult] = field(default_factory=dict)
    category_scores: dict[str, float] = field(default_factory=dict)
    intelligence_score: float = 0.0       # weighted category mean (0..1)
    visible_completion_rate: float = 0.0
    tool_success_rate: float | None = None
    speed: dict = field(default_factory=dict)   # latency/tokens — NEVER in the score
    integration_errors: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    generated_at: str
    settings: dict
    models: list[ModelReport] = field(default_factory=list)
    ranking: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)
