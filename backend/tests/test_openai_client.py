"""The OpenAI client without the network: every failure must become LLMUnavailable,
so the risk gate and replies fall back safely."""

from types import SimpleNamespace

import httpx
import openai
import pytest

from app.llm import LLMUnavailable, OpenAIClient, SafeLLM


def client_returning(fn):
    c = OpenAIClient(api_key="test-key", model="test-model")
    c._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fn)))
    return c


def reply(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_json_purposes_request_a_json_object_and_text_only_gets_redacted_input():
    calls = []
    c = client_returning(lambda **kw: calls.append(kw) or reply('{"risk_level": "low"}'))
    SafeLLM(c).complete("risk prompt JSON", "my name is John Doe, I feel fine", purpose="risk")
    SafeLLM(c).complete("reply prompt", "hello", purpose="reply")

    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[1]["response_format"] is None
    assert (calls[0]["timeout"], calls[1]["timeout"]) == (5.0, 8.0)  # per-purpose, reaches the SDK
    assert "John Doe" not in calls[0]["messages"][1]["content"]
    assert calls[0]["model"] == "test-model"


@pytest.mark.parametrize("error", [
    openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com")),
    openai.APITimeoutError(request=httpx.Request("POST", "https://api.openai.com")),
    openai.RateLimitError("slow down", response=httpx.Response(429, request=httpx.Request("POST", "https://x")),
                          body=None),
    openai.AuthenticationError("bad key", response=httpx.Response(401, request=httpx.Request("POST", "https://x")),
                               body=None),
])
def test_sdk_errors_become_llm_unavailable(error):
    def boom(**_):
        raise error
    with pytest.raises(LLMUnavailable):
        client_returning(boom).complete("s", "u")


def test_empty_answer_is_an_outage_not_an_empty_reply():
    with pytest.raises(LLMUnavailable):
        client_returning(lambda **_: reply("")).complete("s", "u")


def test_outage_routes_patient_to_clinic(cipher, repo, guest_session):
    from app.pipeline import handle_patient_message

    def boom(**_):
        raise openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com"))
    llm = SafeLLM(client_returning(boom))
    result = handle_patient_message(repo, llm, cipher, guest_session.id, "what are your opening hours?",
                                    extractor_llm=llm)
    assert result.risk.risk_level.label == "med" and result.show_send_to_clinic
    assert not result.advice_generated and "can't answer reliably" in result.reply
