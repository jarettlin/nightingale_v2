"""The deterministic half of the risk eval, run on every test pass.

The model's half (scripts/eval_risk.py) costs API calls and isn't perfectly
repeatable, so it's run by hand when the prompt or model changes. These checks
need no network and guard the floor the rules guarantee on their own."""

import json
from pathlib import Path

import pytest

from app.intake import RISK_SYSTEM
from app.risk import assess

CASES = [json.loads(line) for line in
         (Path(__file__).parent.parent / "evals" / "risk_cases.jsonl").read_text().splitlines() if line.strip()]


@pytest.mark.parametrize("case", [c for c in CASES if c.get("rules")], ids=lambda c: c["text"][:40])
def test_rules_alone_catch_every_marked_emergency(case):
    # These must be high even if the model is down, slow or wrong
    assert assess(case["text"]).risk_level.label == "high"


@pytest.mark.parametrize("case", [c for c in CASES if c["expect"] == "low"], ids=lambda c: c["text"][:40])
def test_rules_never_turn_a_routine_message_into_an_emergency(case):
    assert assess(case["text"]).risk_level.label in case.get("allow", ["low"])


def test_eval_set_covers_every_level_and_the_brief_phrases():
    levels = {c["expect"] for c in CASES}
    texts = " ".join(c["text"].lower() for c in CASES)
    assert levels == {"low", "med", "high"}
    for phrase in ("crushing chest pain", "difficulty breathing", "heavy bleeding", "want to hurt myself",
                   "chest feels funny"):
        assert phrase in texts


def test_risk_prompt_defines_every_level():
    # Without definitions the model escalated every mention of a medicine (measured: 35/48 routine answered)
    for level in ("- high:", "- med:", "- low:"):
        assert level in RISK_SYSTEM
    assert "not by itself a risk" in RISK_SYSTEM
