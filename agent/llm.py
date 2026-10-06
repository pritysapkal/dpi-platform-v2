"""The only place that talks to Gemini.

The agent loop depends on the small ModelClient interface below, not on the SDK
directly, so tests can swap in a fake model and run the whole loop offline.
"""

import random
import re
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import httpx
from google import genai
from google.genai import errors, types

# Temporary server-side problems: worth waiting and trying again.
TRANSIENT_STATUS_CODES = {429, 500, 503}
MAX_ATTEMPTS = 5  # per model, including the first try
BASE_DELAY_SECONDS = 2.0
MAX_DELAY_SECONDS = 60.0
# An overloaded Gemini can hang ~50s before answering 503. Giving up sooner and
# retrying (as a transient error) recovers faster.
REQUEST_TIMEOUT_SECONDS = 30
TEMPERATURE = 0  # same question -> same tool choices, which keeps evals reproducible


class LLMError(Exception):
    """A model call failed for good (non-transient error, or retries exhausted)."""


class RetriesExhaustedError(LLMError):
    """Every attempt hit a transient error. The only case where the fallback model is tried."""


@dataclass
class ModelTurn:
    """One reply from the model plus which model actually produced it."""
    content: types.Content
    model: str


class ModelClient(Protocol):
    """What the agent loop needs from a model: given the conversation, return the next turn."""
    def generate(self, contents: list[types.Content]) -> ModelTurn: ...


def build_tool_declarations(tool_functions: list[Callable]) -> list[types.Tool]:
    """Turn our Python tools into Gemini function declarations.

    The SDK reads each function's name, type hints and docstring, so tools.py is the
    single source of truth for what the model sees.
    """
    declarations = [
        types.FunctionDeclaration.from_callable_with_api_option(callable=f, api_option="GEMINI_API")
        for f in tool_functions
    ]
    return [types.Tool(function_declarations=declarations)]


def retry_delay_seconds(error: errors.APIError | None, attempt: int) -> float:
    """How long to wait before the next attempt.

    1. The server's own hint wins: an HTTP Retry-After header, or the RetryInfo
       "retryDelay" that Gemini puts in 429 bodies (e.g. "17s").
    2. Otherwise exponential backoff (2s, 4s, 8s, ...) plus random jitter, so many
       clients that failed together don't all retry at the same moment.
    """
    hint = _server_retry_hint(error) if error is not None else None
    if hint is not None:
        return min(hint, MAX_DELAY_SECONDS)
    backoff = BASE_DELAY_SECONDS * 2 ** (attempt - 1)
    return min(backoff, MAX_DELAY_SECONDS) + random.uniform(0, 1)


def _server_retry_hint(error: errors.APIError) -> float | None:
    headers = getattr(error.response, "headers", None) or {}
    retry_after = headers.get("retry-after") if hasattr(headers, "get") else None
    if retry_after and retry_after.strip().isdigit():
        return float(retry_after)

    details = error.details.get("error", {}).get("details", []) if isinstance(error.details, dict) else []
    for detail in details:
        delay = detail.get("retryDelay") if isinstance(detail, dict) else None
        match = re.fullmatch(r"(\d+(?:\.\d+)?)s", delay or "")
        if match:
            return float(match.group(1))
    return None


class GeminiClient:
    """ModelClient backed by the real Gemini API, with retries and an optional fallback model."""

    def __init__(
        self,
        api_key: str,
        model: str,
        tools: list[types.Tool],
        system_instruction: str,
        fallback_model: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        on_event: Callable[[dict], None] | None = None,
        sticky_fallback: bool = False,
        sdk_client=None,
    ):
        # sdk_client lets tests pass a fake in place of the real network client.
        self._client = sdk_client or genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_SECONDS * 1000),  # milliseconds
        )
        self.model = model
        self.fallback_model = fallback_model
        # sticky: once the fallback answered, stay on it for the rest of this question
        # instead of paying the primary's full retry cost again on every turn.
        self.sticky_fallback = sticky_fallback
        self._on_fallback = False
        self.temperature = TEMPERATURE
        self._sleep = sleep  # injectable so retry tests don't really wait
        self._on_event = on_event or (lambda event: None)  # retry/fallback notices for the trace
        self._config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=tools,
            # We run the tool loop ourselves (agent.py), so the SDK must not execute tools.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=self.temperature,
        )

    def generate(self, contents: list[types.Content]) -> ModelTurn:
        if self._on_fallback:
            return self._generate_with_retries(self.fallback_model, contents)
        try:
            return self._generate_with_retries(self.model, contents)
        except RetriesExhaustedError as primary_error:
            if not self.fallback_model:
                raise
            # Visible switch: logged to the trace, and ModelTurn.model says who answered.
            self._on_event({
                "event": "fallback",
                "from_model": self.model,
                "to_model": self.fallback_model,
                "reason": str(primary_error),
                "sticky": self.sticky_fallback,
            })
            self._on_fallback = self.sticky_fallback
            return self._generate_with_retries(self.fallback_model, contents)

    def _generate_with_retries(self, model: str, contents: list[types.Content]) -> ModelTurn:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._client.models.generate_content(
                    model=model, contents=contents, config=self._config
                )
            except (errors.APIError, httpx.TimeoutException) as exc:
                api_error = exc if isinstance(exc, errors.APIError) else None
                if api_error and api_error.code not in TRANSIENT_STATUS_CODES:
                    # 400/401/403/404: retrying cannot help, so fail fast with a clear message.
                    raise LLMError(_explain_fatal(api_error, model)) from exc
                if api_error:
                    reason = f"{api_error.code} {api_error.status}"
                else:
                    reason = f"timeout after {REQUEST_TIMEOUT_SECONDS}s"
                if attempt == MAX_ATTEMPTS:
                    raise RetriesExhaustedError(
                        f"{model}: still failing after {MAX_ATTEMPTS} attempts ({reason})."
                    ) from exc
                delay = retry_delay_seconds(api_error, attempt)
                self._on_event({
                    "event": "retry",
                    "model": model,
                    "attempt": attempt,
                    "reason": reason,
                    "wait_seconds": round(delay, 1),
                })
                self._sleep(delay)
                continue

            if not response.candidates or response.candidates[0].content is None:
                raise LLMError(f"{model} returned no content (finish reason / safety block).")
            # Keep the model's Content object as-is: Gemini 3 models attach "thought
            # signatures" to function calls, which must be sent back unchanged.
            return ModelTurn(content=response.candidates[0].content, model=model)

        raise AssertionError("unreachable")


def _explain_fatal(exc: errors.APIError, model: str) -> str:
    hints = {
        400: "the request was invalid",
        401: "the API key is missing or invalid (check agent/.env)",
        403: "the API key has no access to this model or API",
        404: f"model '{model}' does not exist or is not available to this key (check GEMINI_MODEL)",
    }
    reason = hints.get(exc.code, "non-retryable error")
    return f"Gemini call failed ({exc.code} {exc.status}): {reason}. {exc.message or ''}".strip()
