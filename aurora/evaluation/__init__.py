"""Aurora Intelligence Lab — a fair, repeatable local model evaluation harness.

See docs/INTELLIGENCE_LAB.md. The harness is DB-free by construction: it
cannot read or write Aurora's production conversation history, memories, or
persona, and it never changes model selection or context settings.
"""

from .backends import (
    EvalBackend, EvalMessages, IntegrationError, MockEvalBackend,
    OllamaBackend, OpenAICompatBackend, probe_backend, split_thinking,
)
from .models import (
    Completion, STATE_COMPLETED, STATE_EMPTY, STATE_EMPTY_WITH_THINKING,
    STATE_INTEGRATION_ERROR, RunReport,
)
from .report import export_report, render_markdown
from .runner import IntelligenceLab
from .scenarios import (
    CATEGORY_TITLES, CATEGORY_WEIGHTS, EVAL_PERSONA, build_scenarios,
)

__all__ = [
    "EvalBackend", "EvalMessages", "IntegrationError", "MockEvalBackend",
    "OllamaBackend", "OpenAICompatBackend", "probe_backend", "split_thinking",
    "Completion", "STATE_COMPLETED", "STATE_EMPTY", "STATE_EMPTY_WITH_THINKING",
    "STATE_INTEGRATION_ERROR", "RunReport", "export_report", "render_markdown",
    "IntelligenceLab", "CATEGORY_TITLES", "CATEGORY_WEIGHTS", "EVAL_PERSONA",
    "build_scenarios",
]
