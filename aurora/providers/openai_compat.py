"""OpenAI-compatible chat provider.

Speaks the /v1/chat/completions dialect used by LM Studio, llama.cpp
(llama-server), Ollama, vLLM, and cloud OpenAI-style endpoints. This is
the provider every local setup uses; it supports streaming SSE deltas and
tool calling when the server offers it.

Design notes adapted from both upstreams: Shikigami's openai_compat
provider (base-url normalization, error sentinel) and Luna's LM Studio /
llama-server handling (probe of /v1/models for reachability and model id).
"""

from __future__ import annotations

import json
import re

import httpx

from .base import ChatProvider, ChatResult, ProviderError, ToolCall, ToolSpec

_LLM_ERROR = "[LLM Error: ...]"  # sentinel kept from Shikigami's design


class OpenAICompatProvider(ChatProvider):
    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        model: str = "",
        api_key: str = "",
        temperature: float = 0.8,
        max_tokens: int = 1024,
        timeout: float = 120.0,
    ) -> None:
        # Strip a trailing /chat/completions if someone pasted a full URL.
        base = re.sub(r"/chat/completions/?$", "", base_url.strip()).rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        self.base_url = base
        self.model = model
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            base_url=base, headers=headers, timeout=httpx.Timeout(timeout, connect=10.0)
        )
        self.temperature = temperature
        self.max_tokens = max_tokens

    async def healthcheck(self) -> bool:
        try:
            resp = await self._client.get("/models")
            return resp.status_code < 500
        except httpx.HTTPError:
            return False

    async def model_loaded(self) -> bool | None:
        """True when the configured model id is in the server's /v1/models
        listing, False when the server is up but has nothing loaded (LM
        Studio unloads models; llama.cpp always serves one), None when it
        cannot be determined. Feeds the resource guard."""
        try:
            resp = await self._client.get("/models")
            if resp.status_code >= 500:
                return None
            data = resp.json()
            items = data.get("data", []) if isinstance(data, dict) else data
            if not isinstance(items, list) or not items:
                return None  # empty or odd listing: not evidence of unload
            ids = []
            for item in items:
                if isinstance(item, dict) and item.get("id"):
                    ids.append(str(item["id"]))
                elif isinstance(item, str):
                    ids.append(item)
            if not ids or not self.model:
                return None
            target = self.model.lower()
            return any(target == i.lower() or i.lower().endswith("/" + target) for i in ids)
        except Exception:
            return None

    def supports_tools(self) -> bool:
        # Assume the OpenAI tool dialect; a server that cannot handle it
        # will error on the first tool round and Aurora degrades by
        # retrying without tools (handled by the conversation manager).
        return True

    def _body(self, messages, tools, temperature, max_tokens, stream) -> dict:
        body: dict = {
            "model": self.model or "local-model",
            "messages": messages,
            "stream": stream,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters or {"type": "object", "properties": {}},
                    },
                }
                for t in tools
            ]
        return body

    async def chat(self, messages, tools=None, temperature=None, max_tokens=None, on_delta=None) -> ChatResult:
        body = self._body(messages, tools, temperature, max_tokens, stream=on_delta is not None)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return await self._chat_once(body, on_delta)
            except httpx.HTTPStatusError as exc:
                # Retry rate limits / transient server errors with backoff
                # (the same resilience a local server needs while loading a model).
                status = exc.response.status_code
                if status not in (429, 500, 502, 503, 504):
                    raise ProviderError(f"{_LLM_ERROR[:-4]}{exc})") from exc
                last_error = exc
            except httpx.HTTPError as exc:
                last_error = exc
            if attempt < 2:
                import asyncio as _a

                await _a.sleep((attempt + 1) * 2.5)
        raise ProviderError(f"{_LLM_ERROR[:-4]}{last_error})") from last_error

    async def _chat_once(self, body: dict, on_delta) -> ChatResult:
        stream = body.get("stream") is True
        if not stream:
            resp = await self._client.post("/chat/completions", json=body)
            resp.raise_for_status()
            data = resp.json()
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            text = message.get("content") or ""
            calls = [
                ToolCall(name=c["function"]["name"], arguments=c["function"].get("arguments", "{}"))
                for c in (message.get("tool_calls") or [])
                if isinstance(c, dict) and c.get("function")
            ]
            return ChatResult(text=text, tool_calls=calls, finish_reason=choice.get("finish_reason", "stop"))
        # streaming path
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        finish = "stop"
        async with self._client.stream("POST", "/chat/completions", json=body) as resp:
            resp.raise_for_status()
            buf_tool_names: dict[int, str] = {}
            buf_tool_args: dict[int, list[str]] = {}
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content") or ""
                if piece:
                    text_parts.append(piece)
                    on_delta(piece)
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        buf_tool_names[idx] = fn["name"]
                    if fn.get("arguments"):
                        buf_tool_args.setdefault(idx, []).append(fn["arguments"])
                if choices[0].get("finish_reason"):
                    finish = choices[0]["finish_reason"]
        for idx, name in buf_tool_names.items():
            calls.append(ToolCall(name=name, arguments="".join(buf_tool_args.get(idx, [])) or "{}"))
        return ChatResult(text="".join(text_parts), tool_calls=calls, finish_reason=finish)
        # HTTPError propagates to chat(), which retries and finally wraps in ProviderError.

    async def close(self) -> None:
        await self._client.aclose()
