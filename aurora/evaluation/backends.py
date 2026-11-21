"""Model backends for the Intelligence Lab.

Three backends share one normalized `Completion` result so the runner and
graders never care where the model lives:

* OllamaBackend — native /api/chat + /api/show + /api/ps. Ollama exposes
  reasoning models' thinking in `message.thinking` (and some models inline
  `<think>…</think>` in the content) and rich timing metadata
  (prompt_eval_duration, eval_count/eval_duration, load_duration).
* OpenAICompatBackend — the LM Studio / llama.cpp / vLLM dialect. Thinking
  arrives as `reasoning_content` (DeepSeek-style servers) or inline
  `<think>` blocks; both are separated from the visible text.
* MockEvalBackend — scripted completions for offline validation of the
  whole pipeline (scoring, empty-response accounting, report generation).

The fairness rules are implemented here and in the runner:
  * visible == "" NEVER counts as a pass, with or without thinking;
  * reasoning/thinking channels are inspected separately from visible text;
  * transport/server failures raise IntegrationError — a *provider
    integration* failure, reported separately from model quality;
  * identical prompts and identical settings go to every model; per-model
    effective context is READ from the server, never changed.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from abc import ABC, abstractmethod

import httpx

from .models import Completion


class IntegrationError(RuntimeError):
    """Endpoint unreachable / HTTP error / timeout — not a model-quality signal."""


_THINK_RE = re.compile(r"<think>(.*?)</think>", re.S | re.I)
_THINK_OPEN_RE = re.compile(r"^<think>(.*)", re.S | re.I)


def split_thinking(content: str) -> tuple[str, str]:
    """Separate inline <think>…</think> blocks from the visible text.
    Returns (visible, thinking)."""
    thinking_parts: list[str] = []

    def _collect(m):
        thinking_parts.append(m.group(1))
        return ""

    text = _THINK_RE.sub(_collect, content)
    m = _THINK_OPEN_RE.match(content)
    if m and not _THINK_RE.search(content):
        # unterminated <think> — everything after the tag is reasoning-only
        thinking_parts.append(m.group(1))
        text = ""
    return text.strip(), "\n".join(thinking_parts).strip()


class EvalBackend(ABC):
    name = "base"

    def __init__(self, model: str) -> None:
        self.model = model

    @abstractmethod
    async def complete(self, messages: list[dict], tools: list[dict] | None = None,
                       temperature: float | None = None,
                       max_tokens: int | None = None) -> Completion:
        ...

    @abstractmethod
    async def inspect(self) -> dict:
        """Model metadata: effective/advertised context, memory, versions.
        Read-only — this harness never changes any server setting."""

    async def close(self) -> None:
        return None


class OllamaBackend(EvalBackend):
    name = "ollama"

    def __init__(self, model: str, base_url: str = "http://127.0.0.1:11434",
                 timeout: float = 600.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(model)
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0),
                                         transport=transport)

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None) -> Completion:
        body: dict = {"model": self.model, "messages": messages, "stream": False}
        options: dict = {}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        if options:
            body["options"] = options
        if tools:
            body["tools"] = tools
        t0 = time.perf_counter()
        try:
            resp = await self._client.post(f"{self.base_url}/api/chat", json=body)
            resp.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            raise IntegrationError(f"Ollama endpoint failed: {exc}") from exc
        wall = time.perf_counter() - t0
        try:
            data = resp.json()
        except ValueError as exc:
            raise IntegrationError(f"Ollama returned non-JSON: {exc}") from exc
        message = data.get("message") or {}
        visible, inline_think = split_thinking(message.get("content") or "")
        thinking = (message.get("thinking") or "") or inline_think
        calls = []
        for c in message.get("tool_calls") or []:
            fn = c.get("function") or {}
            args = fn.get("arguments", {})
            calls.append({"name": fn.get("name", ""),
                          "arguments": args if isinstance(args, str) else json.dumps(args)})
        # Ollama reports nanoseconds.
        prompt_eval_s = data.get("prompt_eval_duration", 0) / 1e9
        eval_s = data.get("eval_duration", 0) / 1e9
        return Completion(
            visible=visible, thinking=thinking, tool_calls=calls,
            timing={
                "wall_s": round(wall, 3),
                "prompt_eval_s": round(prompt_eval_s, 3),
                "generation_s": round(eval_s, 3),
                "ttft_s": round(max(wall - eval_s, 0.0), 3),  # approx: total − generation
                "prompt_tokens": data.get("prompt_eval_count"),
                "output_tokens": data.get("eval_count"),
                "load_s": round((data.get("load_duration") or 0) / 1e9, 3),
                "tokens_per_s": round(data.get("eval_count", 0) / eval_s, 2) if eval_s else None,
            },
        )

    async def inspect(self) -> dict:
        info: dict = {"backend": self.name, "model": self.model}
        try:
            resp = await self._client.post(f"{self.base_url}/api/show",
                                           json={"model": self.model})
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            info["error"] = f"inspect failed: {exc}"
            return info
        model_info = data.get("model_info") or {}
        advertised = sorted({v for k, v in model_info.items()
                             if "context_length" in k and isinstance(v, int)})
        params = data.get("parameters") or ""
        num_ctx = None
        m = re.search(r"num_ctx\s+(\d+)", params)
        if m:
            num_ctx = int(m.group(1))
        info.update({
            "advertised_context_lengths": advertised,
            "num_ctx_parameter": num_ctx,
            "effective_context": num_ctx or (advertised[0] if advertised else None),
            "family": model_info.get("general.architecture"),
            "parameter_count": model_info.get("general.parameter_count"),
            "quantization": model_info.get("general.file_type"),
        })
        # memory of the loaded model, when /api/ps reports it
        try:
            ps = await self._client.get(f"{self.base_url}/api/ps")
            ps.raise_for_status()
            for entry in ps.json().get("models") or []:
                if entry.get("name") == self.model or entry.get("model") == self.model:
                    size = entry.get("size") or entry.get("size_vram")
                    if size:
                        info["loaded_size_bytes"] = size
                    info["context_from_ps"] = entry.get("context_length")
        except (httpx.HTTPError, OSError, ValueError):
            pass
        return info

    async def close(self) -> None:
        await self._client.aclose()


class OpenAICompatBackend(EvalBackend):
    name = "openai_compat"

    def __init__(self, model: str, base_url: str, api_key: str = "",
                 timeout: float = 600.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(model)
        base = re.sub(r"/chat/completions/?$", "", base_url.strip()).rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        self._client = httpx.AsyncClient(
            base_url=base,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
            timeout=httpx.Timeout(timeout, connect=10.0), transport=transport)

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None) -> Completion:
        body: dict = {"model": self.model, "messages": messages, "stream": False,
                      "temperature": temperature if temperature is not None else 0.7}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = tools
        t0 = time.perf_counter()
        try:
            resp = await self._client.post("/chat/completions", json=body)
            resp.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            raise IntegrationError(f"OpenAI-compatible endpoint failed: {exc}") from exc
        wall = time.perf_counter() - t0
        try:
            data = resp.json()
        except ValueError as exc:
            raise IntegrationError(f"Endpoint returned non-JSON: {exc}") from exc
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        visible, inline = split_thinking(message.get("content") or "")
        reasoning = (message.get("reasoning_content") or "") or ""
        thinking = "\n".join(x for x in (reasoning, inline) if x)
        calls = [
            {"name": c["function"].get("name", ""),
             "arguments": c["function"].get("arguments", "{}")}
            for c in (message.get("tool_calls") or [])
            if isinstance(c, dict) and c.get("function")
        ]
        usage = data.get("usage") or {}
        return Completion(
            visible=visible, thinking=thinking, tool_calls=calls,
            timing={
                "wall_s": round(wall, 3),
                "prompt_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
                "tokens_per_s": (round(usage.get("completion_tokens", 0) / wall, 2)
                                 if wall and usage.get("completion_tokens") else None),
            },
        )

    async def inspect(self) -> dict:
        info: dict = {"backend": self.name, "model": self.model}
        try:
            resp = await self._client.get("/models")
            resp.raise_for_status()
            items = resp.json().get("data", [])
            for item in items:
                if isinstance(item, dict) and str(item.get("id", "")).lower() == self.model.lower():
                    ctx = item.get("context_length") or item.get("max_context_length")
                    if ctx:
                        info["advertised_context_lengths"] = [int(ctx)]
                        info["effective_context"] = int(ctx)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            info["error"] = f"inspect failed: {exc}"
        return info

    async def close(self) -> None:
        await self._client.aclose()


class MockEvalBackend(EvalBackend):
    """Scripted backend for offline validation. `handler(scenario_id, turn_no,
    trial_no, messages) -> Completion | None` (None → default good reply).
    Supports the same normalized Completion as the real backends, and raises
    IntegrationError when scripted, so the full fairness pipeline — empty
    responses, thinking-only answers, transport failures — is exercised
    without any Ollama server."""

    name = "mock"

    def __init__(self, model: str, handler=None,
                 context_info: dict | None = None) -> None:
        super().__init__(model)
        self.handler = handler
        self.context_info = context_info or {
            "advertised_context_lengths": [131072],
            "num_ctx_parameter": 8192,
            "effective_context": 8192,
            "loaded_size_bytes": 2_500_000_000,
        }
        self.calls: list[dict] = []

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None) -> Completion:
        meta = getattr(messages, "eval_meta", None) or {}
        self.calls.append({"user": messages[-1]["content"] if messages else "",
                           "n": len(messages), "tools": bool(tools),
                           "temperature": temperature, "max_tokens": max_tokens})
        if self.handler is not None:
            out = self.handler(meta.get("scenario_id", ""), meta.get("turn_no", 0),
                               meta.get("trial_no", 0), messages, tools)
            if isinstance(out, Completion):
                return out
            if isinstance(out, str):
                return Completion(visible=out,
                                  timing={"wall_s": 0.5, "prompt_eval_s": 0.1,
                                          "generation_s": 0.4, "ttft_s": 0.1,
                                          "prompt_tokens": 100, "output_tokens": 40,
                                          "tokens_per_s": 100.0})
        return Completion(visible="This is a mock reply with enough text to pass "
                                  "most length checks.",
                          timing={"wall_s": 0.5, "prompt_eval_s": 0.1,
                                  "generation_s": 0.4, "ttft_s": 0.1,
                                  "prompt_tokens": 100, "output_tokens": 40,
                                  "tokens_per_s": 100.0})

    async def inspect(self) -> dict:
        return {"backend": self.name, "model": self.model, **self.context_info}


class EvalMessages(list):
    """Message list carrying harness metadata (scenario/turn/trial) without
    polluting the OpenAI-style payload. Real backends ignore the metadata;
    the mock backend uses it to script responses per scenario."""

    def __init__(self, rows: list[dict], meta: dict | None = None) -> None:
        super().__init__(rows)
        self.eval_meta: dict = meta or {}


def attach_eval_meta(messages: list[dict], meta: dict) -> list[dict]:
    """Return a message list carrying harness metadata (used by the mock)."""
    if isinstance(messages, EvalMessages):
        messages.eval_meta.update(meta)
        return messages
    return EvalMessages(messages, meta)


async def probe_backend(base_url: str, backend: str) -> bool:
    """Cheap reachability probe used by the CLI before a real run."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as c:
            if backend == "ollama":
                r = await c.get(f"{base_url.rstrip('/')}/api/tags")
            else:
                r = await c.get(f"{base_url.rstrip('/')}/v1/models")
            return r.status_code < 500
    except (httpx.HTTPError, OSError):
        return False
