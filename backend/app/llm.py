"""LLM access. Nothing else in the app talks to a model directly.

`LLMClient` is the raw model interface (OpenAI in production, FakeLLM in tests).
`SafeLLM` wraps it and is the only thing the rest of the app is handed:
it redacts every piece of patient text before the client sees it, and refuses
to call the model at all if redaction fails.
"""

import time
from dataclasses import dataclass, field
from typing import Protocol

from app.logs import log_event
from app.redaction import redact_or_raise


class LLMClient(Protocol):
    def complete(self, system: str, user: str, timeout_s: float = 10.0, json_mode: bool = False) -> str: ...


class LLMUnavailable(Exception):
    """Timeout, network error or bad response. Callers fall back to safe templates."""


# Calls whose answer the server parses, so the model must return a JSON object
JSON_PURPOSES = {"risk", "extract", "verify"}

# Per-attempt timeouts. A stalled request is cut short and retried once instead
# of making the patient wait. Measured on gpt-4.1-mini (2026-11-08): risk calls
# take 0.9s median, 1.7s at worst over 30 real messages; the reply and claim
# check about 0.8-1.4s. Each limit is roughly twice the slowest healthy call.
# Seen live: a stalled risk call took 6.7s under the old 5s limit. Extraction
# can be generous, because the reply never waits more than 1.5s for it.
TIMEOUTS = {"risk": 3.0, "reply": 5.0, "extract": 6.0, "verify": 3.0}
DEFAULT_TIMEOUT = 8.0


class OpenAIClient:
    """OpenAI Chat Completions. Receives only redacted text (SafeLLM sees to that).
    Any SDK error becomes LLMUnavailable, so callers use their safe fallbacks."""

    def __init__(self, api_key: str, model: str, timeout_s: float = 15.0):
        import openai
        self._openai = openai
        # One retry: a patient is waiting, and every failure has a safe fallback
        self._client = openai.OpenAI(api_key=api_key, timeout=timeout_s, max_retries=1)
        self.model = model

    def complete(self, system: str, user: str, timeout_s: float = 15.0, json_mode: bool = False) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={"type": "json_object"} if json_mode else None,
                # Judgements the server parses (risk, extraction) should be repeatable
                temperature=0 if json_mode else None,
                timeout=timeout_s,
            )
        except self._openai.OpenAIError as exc:
            raise LLMUnavailable(type(exc).__name__) from exc
        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise LLMUnavailable("empty response")
        return content


@dataclass
class FakeLLM:
    """Test double: records every prompt and returns scripted replies in order."""

    replies: list[str] = field(default_factory=list)
    prompts: list[dict] = field(default_factory=list)
    fail: bool = False

    def complete(self, system: str, user: str, timeout_s: float = 10.0, json_mode: bool = False) -> str:
        self.prompts.append({"system": system, "user": user, "json_mode": json_mode})
        if self.fail:
            raise LLMUnavailable("fake timeout")
        return self.replies.pop(0) if self.replies else "{}"

    @property
    def last_prompt(self) -> str:
        p = self.prompts[-1]
        return p["system"] + "\n" + p["user"]


class UnconfiguredLLM:
    """No model configured yet: every call is an outage, so the app runs in its
    safe fallback mode (rules-only risk, templates, offer to send to the clinic)."""

    def complete(self, system: str, user: str, timeout_s: float = 10.0, json_mode: bool = False) -> str:
        raise LLMUnavailable("no model configured")


class SafeLLM:
    def __init__(self, client: LLMClient):
        self._client = client

    def complete(self, system: str, patient_text: str, purpose: str,
                 known_identifiers: list[str] | None = None) -> str:
        # Raises RedactionError before any network call if the text can't be made safe
        safe_text = redact_or_raise(patient_text, known_identifiers)
        started = time.perf_counter()
        try:
            reply = self._client.complete(system, safe_text, timeout_s=TIMEOUTS.get(purpose, DEFAULT_TIMEOUT),
                                          json_mode=purpose in JSON_PURPOSES)
        except LLMUnavailable:
            log_event("llm_unavailable", purpose=purpose, latency_ms=_ms(started))
            raise
        except Exception as exc:
            log_event("llm_unavailable", purpose=purpose, error_type=type(exc).__name__, latency_ms=_ms(started))
            raise LLMUnavailable(str(type(exc).__name__)) from exc
        log_event("llm_call", purpose=purpose, input_chars=len(safe_text), latency_ms=_ms(started))
        return reply


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
