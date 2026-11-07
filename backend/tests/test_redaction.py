import logging

import pytest

from app.llm import FakeLLM, SafeLLM
from app.redaction import PLACEHOLDER, RedactionError, redact


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.INFO, logger="nightingale")
    return caplog


def test_brief_example_is_redacted_before_llm_and_absent_from_logs(logs):
    fake = FakeLLM(replies=["ok"])
    SafeLLM(fake).complete("system prompt", "My name is John Doe and my IC is S1234567A.", purpose="reply")

    sent = fake.last_prompt
    assert "John Doe" not in sent
    assert "S1234567A" not in sent
    assert sent.count(PLACEHOLDER) == 2
    assert "John Doe" not in logs.text
    assert "S1234567A" not in logs.text


@pytest.mark.parametrize("text, secret", [
    ("my ic is 900101-14-5678", "900101-14-5678"),
    ("ic 900101145678 thanks", "900101145678"),
    ("call me on +60 12-345 6789", "345 6789"),
    ("my number is 0123456789", "0123456789"),
    ("sg number 9123 4567", "9123 4567"),
    ("email me at sarah.tan@example.com", "sarah.tan@example.com"),
    ("dm me @sarah.t on insta", "@sarah.t"),
    ("born 01/01/1990", "01/01/1990"),
    ("passport A12345678", "A12345678"),
    ("I'm Sarah Tan and I have a headache", "Sarah Tan"),
    ("this is Puan Siti Aminah", "Siti Aminah"),
    ("my husband Ahmad bin Ali is worried", "Ahmad bin Ali"),
    ("my name is john doe and I feel sick", "john doe"),
])
def test_identifiers_are_removed(text, secret):
    result = redact(text)
    assert result.ok
    assert secret not in result.redacted_text


def test_clinical_content_is_kept():
    text = "I've had a headache for 3 days and I take Advil 200mg twice a day since last week"
    result = redact(text)
    assert result.redacted_text == text
    assert result.entities == []


def test_feelings_after_im_are_not_mistaken_for_names():
    assert redact("I'm Worried about my cycle").redacted_text == "I'm Worried about my cycle"


def test_known_identifiers_are_always_removed():
    result = redact("hi it's zara here, any slots friday?", known_identifiers=["zara"])
    assert "zara" not in result.redacted_text.lower()


def test_entities_record_positions_not_values():
    result = redact("My name is John Doe")
    assert result.entities == [{"kind": "NAME", "start": 11, "end": 19}]


def test_failure_closes_and_llm_is_never_called(monkeypatch):
    import app.redaction as r
    monkeypatch.setattr(r, "_find_id_spans", lambda text: 1 / 0)
    fake = FakeLLM()

    assert redact("My IC is S1234567A").ok is False
    with pytest.raises(RedactionError):
        SafeLLM(fake).complete("sys", "My IC is S1234567A", purpose="reply")
    assert fake.prompts == []
