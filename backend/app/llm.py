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
    def complete(self, system: str, user: str, timeout_s: float = 10.0) -> str: ...


class LLMUnavailable(Exception):
    """Timeout, network error or bad response. Callers fall back to safe templates."""


@dataclass
class FakeLLM:
    """Test double: records every prompt and returns scripted replies in order."""

    replies: list[str] = field(default_factory=list)
    prompts: list[dict] = field(default_factory=list)
    fail: bool = False

    def complete(self, system: str, user: str, timeout_s: float = 10.0) -> str:
        self.prompts.append({"system": system, "user": user})
        if self.fail:
            raise LLMUnavailable("fake timeout")
        return self.replies.pop(0) if self.replies else "{}"

    @property
    def last_prompt(self) -> str:
        p = self.prompts[-1]
        return p["system"] + "\n" + p["user"]


class SafeLLM:
    def __init__(self, client: LLMClient):
        self._client = client

    def complete(self, system: str, patient_text: str, purpose: str,
                 known_identifiers: list[str] | None = None) -> str:
        # Raises RedactionError before any network call if the text can't be made safe
        safe_text = redact_or_raise(patient_text, known_identifiers)
        try:
            reply = self._client.complete(system, safe_text)
        except LLMUnavailable:
            log_event("llm_unavailable", purpose=purpose)
            raise
        except Exception as exc:
            log_event("llm_unavailable", purpose=purpose, error_type=type(exc).__name__)
            raise LLMUnavailable(str(type(exc).__name__)) from exc
        log_event("llm_call", purpose=purpose, input_chars=len(safe_text))
        return reply
