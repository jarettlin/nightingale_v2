import json

import pytest

from app.intake import REPLY_SYSTEM, handle_turn
from app.llm import FakeLLM, SafeLLM
from app.risk import Risk, assess

LOW_OPINION = json.dumps({"risk_level": "low", "reason": "fine", "confidence": "high"})


def advice_calls(fake: FakeLLM) -> list[dict]:
    """Prompts that asked the model for a patient-facing answer."""
    marker = REPLY_SYSTEM.split("{")[0]
    return [p for p in fake.prompts if p["system"].startswith(marker)]


def test_crushing_chest_pain_brief_example():
    # Even if the model wrongly says "low", the rules keep it high
    fake = FakeLLM(replies=[LOW_OPINION, "take an aspirin"])
    result = handle_turn("I have crushing chest pain.", SafeLLM(fake))

    assert result.risk.risk_level == Risk.HIGH
    assert result.risk.escalation_required is True
    assert result.advice_generated is False
    assert advice_calls(fake) == []
    assert "aspirin" not in result.reply
    assert "999" in result.reply
    assert result.show_send_to_clinic


@pytest.mark.parametrize("text", [
    "I have crushing chest pain",
    "I'm having difficulty breathing",
    "there's heavy bleeding since this morning",
    "I want to hurt myself",
    "i cant breathe properly",
    "I keep thinking about ending my life, I don't want to live",
    "I've been soaking through pads every hour",
    "severe abdominal pain after my egg retrieval",
    "My throat is swelling after the injection",
])
def test_must_never_miss_high(text):
    assert assess(text).risk_level == Risk.HIGH


@pytest.mark.parametrize("text", [
    "my chest feels funny",
    "I feel a bit dizzy since yesterday",
    "some spotting today",
    "really bloated since the retrieval",
])
def test_ambiguous_symptoms_escalate(text):
    risk = assess(text)
    assert risk.risk_level == Risk.MED
    assert risk.escalation_required


def test_negated_high_is_med_not_low():
    risk = assess("no chest pain, just a mild headache")
    assert risk.risk_level == Risk.MED


@pytest.mark.parametrize("text", [
    "what are your opening hours?",
    "how much does egg freezing cost?",
    "what is an AMH test",
])
def test_low_risk_gets_a_grounded_answer(text):
    from app.knowledge import default_kb
    from tests.conftest import RoutingLLM
    result = handle_turn(text, SafeLLM(RoutingLLM()), sources=default_kb().search(text))
    assert result.risk.risk_level == Risk.LOW
    assert result.advice_generated and result.citations


def test_low_risk_without_clinic_sources_is_honest_not_improvised():
    fake = FakeLLM(replies=[LOW_OPINION])
    result = handle_turn("I had a headache for 3 days", SafeLLM(fake), sources=[])
    assert result.risk.risk_level == Risk.LOW and not result.advice_generated
    assert "rather not guess" in result.reply
    assert advice_calls(fake) == []  # the model was never asked to make something up


def test_llm_can_raise_risk_but_not_lower_it():
    raised = assess("I feel off today", llm_opinion={"risk_level": "high", "reason": "x", "confidence": "med"})
    assert raised.risk_level == Risk.HIGH
    lowered = assess("crushing chest pain", llm_opinion={"risk_level": "low", "reason": "x", "confidence": "high"})
    assert lowered.risk_level == Risk.HIGH


def test_llm_down_routes_to_clinic_without_advice():
    fake = FakeLLM(fail=True)
    result = handle_turn("what are your opening hours?", SafeLLM(fake))
    assert result.risk.risk_level == Risk.MED
    assert result.risk.source == "rules+llm_unavailable"
    assert result.advice_generated is False
    assert result.show_send_to_clinic


@pytest.mark.parametrize("text, intent", [
    ("do I have endometriosis?", "wants_diagnosis"),
    ("should I stop taking my progesterone?", "medication_change"),
])
def test_diagnosis_and_medication_requests_go_to_clinic(text, intent):
    fake = FakeLLM(replies=[LOW_OPINION, "you probably have..."])
    result = handle_turn(text, SafeLLM(fake))
    assert intent in result.risk.intents
    assert result.risk.escalation_required
    assert advice_calls(fake) == []


def test_every_assessment_has_reason_confidence_and_timestamp():
    risk = assess("hello")
    assert risk.risk_reason and risk.confidence in {"low", "med", "high"}
    assert "T" in risk.risk_provenance
