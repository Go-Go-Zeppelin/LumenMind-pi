"""Strict, bounded Anthropic invocation shared by memory model pipelines."""
from __future__ import annotations

import asyncio
import json
import time

from .metrics import observe_model
import math
from typing import Any, Optional


class MemoryModelError(RuntimeError):
    pass


class MemoryModelUnavailable(MemoryModelError):
    pass


class MemoryModelBudgetError(MemoryModelError):
    pass


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MemoryModelError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise MemoryModelError(f"Invalid JSON constant: {value}")


def parse_model_json(response: Any) -> dict:
    """Reject truncation, tool use, non-text blocks, prose and ambiguous JSON."""
    if getattr(response, "stop_reason", None) != "end_turn":
        raise MemoryModelError("Memory model did not complete an end_turn response")
    content = getattr(response, "content", None)
    if not isinstance(content, list) or not content:
        raise MemoryModelError("Memory model returned no text blocks")
    texts = []
    for block in content:
        if getattr(block, "type", None) != "text" or not isinstance(getattr(block, "text", None), str):
            raise MemoryModelError("Memory model may return text blocks only")
        texts.append(block.text)
    try:
        value = json.loads("".join(texts), object_pairs_hook=_strict_object,
                           parse_constant=_reject_constant)
    except (ValueError, TypeError) as error:
        raise MemoryModelError("Memory model returned invalid standalone JSON") from error
    if not isinstance(value, dict):
        raise MemoryModelError("Memory model must return one JSON object")
    return value


class MemoryModelClient:
    """The client is created only on a configured, budgeted model call.

    UTF-8 bytes provide a conservative token bound; the allowance covers message
    framing, and output tokens are reserved before invoking the model. Callers
    may supply a smaller configured context window for their provider model.
    """
    def __init__(self, api_key: str = "", base_url: Optional[str] = None,
                 model: str = "", timeout_seconds: float = 30,
                 max_input_chars: int = 24_000, max_output_tokens: int = 4_096,
                 context_window_tokens: int = 65_536, client: Any = None, purpose: str = "fact"):
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or max_input_chars <= 0 or max_output_tokens <= 0:
            raise ValueError("Memory model limits must be positive")
        if context_window_tokens <= max_output_tokens + 1024:
            raise ValueError("Memory model context window must reserve room for input")
        self.purpose = purpose
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_input_chars = max_input_chars
        self.max_output_tokens = max_output_tokens
        self.context_window_tokens = context_window_tokens
        self._client = client
        self._owns_client = client is None

    @property
    def configured(self) -> bool:
        return bool(self.model.strip() and (self.api_key.strip() or self._client is not None))

    def _check_budget(self, system: str, prompt: str) -> None:
        if len(system) + len(prompt) > self.max_input_chars:
            raise MemoryModelBudgetError("Memory model input exceeds the character budget")
        conservative_input_tokens = len(system.encode("utf-8")) + len(prompt.encode("utf-8")) + 1024
        if conservative_input_tokens + self.max_output_tokens > self.context_window_tokens:
            raise MemoryModelBudgetError("Memory model input plus output reserve exceeds its context window")

    async def generate_json(self, system: str, prompt: str) -> dict:
        started = time.perf_counter()
        outcome = "failed"
        usage = None
        try:
            self._check_budget(system, prompt)
            if not self.configured:
                raise MemoryModelUnavailable("Memory model API key/model is not configured")
            if self._client is None:
                from anthropic import AsyncAnthropic
                kwargs = {"api_key": self.api_key, "timeout": self.timeout_seconds, "max_retries": 0}
                if self.base_url:
                    kwargs["base_url"] = self.base_url
                self._client = AsyncAnthropic(**kwargs)
            response = await asyncio.wait_for(self._client.messages.create(
                model=self.model, system=system, max_tokens=self.max_output_tokens,
                temperature=0, messages=[{"role": "user", "content": prompt}],
            ), timeout=self.timeout_seconds)
            usage = getattr(response, "usage", None)
            result = parse_model_json(response)
            outcome = "success"
            return result
        finally:
            observe_model(self.purpose, outcome, time.perf_counter() - started, usage)

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.close()
            self._client = None
