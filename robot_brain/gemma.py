"""Gemma 4 brain over REST. No GPU, no local weights.

Two GPU-free providers behind one interface:

- ``aistudio``  -> Google AI Studio (generativelanguage API, ``:generateContent``).
- ``openrouter`` -> OpenRouter (OpenAI-compatible ``/chat/completions``).

The primary path is a faithful Python port of ``skillos_x_robot``'s ``GeminiBackend``
(OpenAI-style messages <-> Gemini ``contents`` translation, both directions).

``generate(messages, image=None) -> str`` matches odyssey's ``TextGenerator`` protocol,
so an instance drops straight into odyssey's ``LLMPlanner``. ``generate_full(...)`` exposes
tool-calls and usage for the 2D pilot's function-calling loop.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

AISTUDIO_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class GemmaError(RuntimeError):
    """Raised when the brain cannot produce a completion."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class GenerateResult:
    message: Optional[str]
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)


# --- messages ---------------------------------------------------------------
# A message is an OpenAI-style dict:
#   {"role": "system"|"user"|"assistant"|"tool", "content": str|None,
#    "tool_calls": [ToolCall-ish], "tool_call_id": str}
# A tool definition is OpenAI-style:
#   {"type": "function", "function": {"name","description","parameters": {...}}}


class GemmaBrain:
    """Base contract shared by both providers."""

    model: str

    def generate(self, messages: list[dict], image: Any = None) -> str:
        """odyssey TextGenerator: return assistant text only."""
        return self.generate_full(messages, image=image).message or ""

    def generate_full(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        tool_choice: str = "auto",
        image: Any = None,
    ) -> GenerateResult:  # pragma: no cover - overridden
        raise NotImplementedError


# --- Google AI Studio -------------------------------------------------------


class AiStudioBrain(GemmaBrain):
    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        max_tokens: int = 8192,
        temperature: float = 0.3,
        max_retries: int = 3,
        timeout: Optional[float] = None,
    ):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.model = model or os.environ.get("GEMMA_MODEL", "gemma-4-26b-a4b-it")
        timeout = timeout if timeout is not None else float(os.environ.get("GEMMA_TIMEOUT", "120"))
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_retries = max_retries
        self._client = httpx.Client(timeout=timeout)
        if not self.api_key:
            raise GemmaError(
                "AI Studio API key required. Set GEMINI_API_KEY or pass api_key."
            )

    def generate_full(self, messages, tools=None, tool_choice="auto", image=None):
        body = self._build_body(messages, tools, image)
        url = f"{AISTUDIO_BASE_URL}/models/{self.model}:generateContent?key={self.api_key}"
        json_body = self._post_with_retry(url, {}, body, label="AI Studio")
        return self._parse(json_body)

    # -- Gemini format conversion (ported from GeminiBackend.buildRequestBody) --

    def _build_body(self, messages, tools, image) -> dict:
        body: dict[str, Any] = {
            "generationConfig": {
                "maxOutputTokens": self.max_tokens,
                "temperature": self.temperature,
            }
        }

        system_msg = next((m for m in messages if m.get("role") == "system"), None)
        if system_msg and system_msg.get("content"):
            body["system_instruction"] = {"parts": [{"text": system_msg["content"]}]}

        contents: list[dict] = []
        for msg in messages:
            role = msg.get("role")
            if role == "system":
                continue
            if role == "user":
                parts: list[dict] = [{"text": msg.get("content") or ""}]
                contents.append({"role": "user", "parts": parts})
            elif role == "assistant":
                parts = []
                if msg.get("content"):
                    parts.append({"text": msg["content"]})
                for tc in msg.get("tool_calls") or []:
                    args = _tool_args(tc)
                    parts.append(
                        {"functionCall": {"name": _tool_name(tc), "args": args}}
                    )
                if parts:
                    contents.append({"role": "model", "parts": parts})
            elif role == "tool":
                tool_name = _find_tool_name(messages, msg.get("tool_call_id"))
                try:
                    response_data = json.loads(msg.get("content") or "{}")
                    if not isinstance(response_data, dict):
                        response_data = {"result": response_data}
                except (ValueError, TypeError):
                    response_data = {"result": msg.get("content")}
                fn_part = {
                    "functionResponse": {"name": tool_name, "response": response_data}
                }
                last = contents[-1] if contents else None
                if (
                    last
                    and last["role"] == "user"
                    and last["parts"]
                    and "functionResponse" in last["parts"][0]
                ):
                    last["parts"].append(fn_part)
                else:
                    contents.append({"role": "user", "parts": [fn_part]})

        # Attach an image (base64 PNG or raw bytes) to the last user turn, if given.
        if image is not None:
            data = image if isinstance(image, (bytes, bytearray)) else None
            b64 = (
                base64.b64encode(data).decode("ascii")
                if data is not None
                else str(image)
            )
            user_turn = next(
                (c for c in reversed(contents) if c["role"] == "user"), None
            )
            img_part = {"inline_data": {"mime_type": "image/png", "data": b64}}
            if user_turn:
                user_turn["parts"].append(img_part)
            else:
                contents.append({"role": "user", "parts": [img_part]})

        body["contents"] = contents

        if tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t["function"]["name"],
                            "description": t["function"].get("description", ""),
                            "parameters": t["function"].get("parameters", {}),
                        }
                        for t in tools
                    ]
                }
            ]
        return body

    def _parse(self, json_body: dict) -> GenerateResult:
        candidates = json_body.get("candidates") or []
        if not candidates:
            raise GemmaError(f"AI Studio returned no candidates: {json_body}")
        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = ""
        tool_calls: list[ToolCall] = []
        for i, part in enumerate(parts):
            if part.get("text"):
                text += part["text"]
            fc = part.get("functionCall")
            if fc:
                tool_calls.append(
                    ToolCall(
                        id=fc.get("id") or f"call_{int(time.time()*1000)}_{i}",
                        name=fc["name"],
                        arguments=fc.get("args") or {},
                    )
                )
        finish = candidate.get("finishReason") or "STOP"
        finish_reason = (
            "tool_calls"
            if tool_calls
            else {"STOP": "stop", "MAX_TOKENS": "length"}.get(finish, finish.lower())
        )
        usage = json_body.get("usageMetadata") or {}
        return GenerateResult(
            message=text or None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            model=self.model,
            usage={
                "prompt_tokens": usage.get("promptTokenCount", 0),
                "completion_tokens": usage.get("candidatesTokenCount", 0),
                "total_tokens": usage.get("totalTokenCount", 0),
            },
        )

    def _post_with_retry(self, url, headers, body, label) -> dict:
        return _post_with_retry(self._client, url, headers, body, self.max_retries, label)


# --- OpenRouter -------------------------------------------------------------


class OpenRouterBrain(GemmaBrain):
    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        max_tokens: int = 1024,
        temperature: float = 0.3,
        max_retries: int = 3,
        timeout: Optional[float] = None,
    ):
        timeout = timeout if timeout is not None else float(os.environ.get("GEMMA_TIMEOUT", "120"))
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.model = model or os.environ.get(
            "OPENROUTER_MODEL", "google/gemma-4-26b-a4b-it"
        )
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_retries = max_retries
        self._client = httpx.Client(timeout=timeout)
        if not self.api_key:
            raise GemmaError(
                "OpenRouter API key required. Set OPENROUTER_API_KEY or pass api_key."
            )

    def generate_full(self, messages, tools=None, tool_choice="auto", image=None):
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://github.com/EvolvingAgentsLabs/evolving-robot",
            "X-Title": "evolving-robot",
        }
        json_body = _post_with_retry(
            self._client,
            f"{OPENROUTER_BASE_URL}/chat/completions",
            headers,
            body,
            self.max_retries,
            "OpenRouter",
        )
        choice = (json_body.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tool_calls = [
            ToolCall(
                id=tc.get("id", f"call_{i}"),
                name=tc["function"]["name"],
                arguments=_loads(tc["function"].get("arguments") or "{}"),
            )
            for i, tc in enumerate(msg.get("tool_calls") or [])
        ]
        return GenerateResult(
            message=msg.get("content"),
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason") or "unknown",
            model=json_body.get("model") or self.model,
            usage=json_body.get("usage") or {},
        )


# --- factory + helpers ------------------------------------------------------


def make_brain(provider: Optional[str] = None, **overrides) -> GemmaBrain:
    """Build a brain from ROBOT_PROVIDER (default 'aistudio') or an explicit provider."""
    provider = provider or os.environ.get("ROBOT_PROVIDER", "aistudio")
    if provider == "openrouter":
        return OpenRouterBrain(**overrides)
    if provider in ("aistudio", "gemma", "gemini"):
        return AiStudioBrain(**overrides)
    raise GemmaError(f"Unknown provider: {provider!r} (aistudio|openrouter)")


def _post_with_retry(client, url, headers, body, max_retries, label) -> dict:
    payload = {"Content-Type": "application/json", **headers}
    last_err: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            res = client.post(url, headers=payload, json=body)
            if res.status_code >= 400:
                if res.status_code >= 500 or res.status_code == 429:
                    last_err = GemmaError(f"{label} {res.status_code}: {res.text}")
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise GemmaError(f"{label} {res.status_code}: {res.text}")
            return res.json()
        except httpx.HTTPError as err:
            last_err = err
            if attempt < max_retries - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise GemmaError(f"{label} network error: {err}") from err
    raise last_err or GemmaError(f"{label}: exhausted retries")


def _loads(s: str) -> dict:
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {"value": v}
    except (ValueError, TypeError):
        return {}


def _tool_name(tc: Any) -> str:
    if isinstance(tc, ToolCall):
        return tc.name
    return tc.get("function", {}).get("name") or tc.get("name", "unknown")


def _tool_args(tc: Any) -> dict:
    if isinstance(tc, ToolCall):
        return tc.arguments
    fn = tc.get("function", {})
    raw = fn.get("arguments", tc.get("arguments", {}))
    return _loads(raw) if isinstance(raw, str) else (raw or {})


def _find_tool_name(messages: list[dict], tool_call_id: Optional[str]) -> str:
    if not tool_call_id:
        return "unknown"
    for msg in messages:
        for tc in msg.get("tool_calls") or []:
            tid = tc.id if isinstance(tc, ToolCall) else tc.get("id")
            if tid == tool_call_id:
                return _tool_name(tc)
    return "unknown"
