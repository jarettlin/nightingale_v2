"""LLM access. Nothing else in the app talks to a model directly.

`LLMClient` is the raw model interface (Claude in production, FakeLLM in tests).
`SafeLLM` wraps it and is the only thing the rest of the app is handed:
it redacts every piece of patient text before the client sees it, and refuses
to call the model at all if redaction fails.
"""

from dataclasses import dataclass, field
from typing import Protocol

from app.logs import log_event
from app.redaction import redact_or_raise


class LLMClient(Protocol):
    def complete(self, system: str, user: str, timeout_s: float = 10.0, json_mode: bool = False) -> str: ...


class LLMUnavailable(Exception):
    """Timeout, network error or bad response. Callers fall back to safe templates."""


# Calls whose answer the server parses, so the model must return a JSON object
JSON_PURPOSES = {"risk", "extract"}


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
        try:
            reply = self._client.complete(system, safe_text, json_mode=purpose in JSON_PURPOSES)
        except LLMUnavailable:
            log_event("llm_unavailable", purpose=purpose)
            raise
        except Exception as exc:
            log_event("llm_unavailable", purpose=purpose, error_type=type(exc).__name__)
            raise LLMUnavailable(str(type(exc).__name__)) from exc
        log_event("llm_call", purpose=purpose, input_chars=len(safe_text))
        return reply
