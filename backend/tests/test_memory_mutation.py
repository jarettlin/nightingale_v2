import json

import pytest

from app.extraction import extract, rule_extract
from app.llm import FakeLLM, SafeLLM
from app.memory import InMemoryStore, apply_operations, history, profile


def turn(store, text, message_id, llm=None):
    """One patient message through extraction and memory, like the pipeline will do."""
    result = extract(text, store.current(), llm)
    apply_operations(store, result.operations, message_id, extracted_by=result.source.split()[0])
    return result


def meds(store):
    return [(m["value"], m["status"]) for m in profile(store).get("medication", [])]


# ------------------------------------------------------------ brief example

def test_brief_example_advil_then_stopped():
    store = InMemoryStore()

    turn(store, "I take Advil.", "msg_1")
    assert meds(store) == [("Advil", "active")]

    turn(store, "Actually I stopped last week.", "msg_2")
    assert meds(store) == [("Advil", "stopped")]

    # Both states exist, each linked to the message that caused it
    current = profile(store)["medication"][0]
    chain = history(store, current["id"])
    assert [(v.status, v.provenance_message_id) for v in chain] == [("active", "msg_1"), ("stopped", "msg_2")]
    assert chain[1].supersedes_id == chain[0].id
    assert chain[1].detail["effective"] == "last week"
    assert chain[1].updated_at >= chain[0].updated_at


def test_brief_example_via_llm_operations():
    store = InMemoryStore()
    fake = FakeLLM(replies=[
        json.dumps({"operations": [{"op": "add", "kind": "medication", "value": "Advil", "status": "active"}]}),
        json.dumps({"operations": [{"op": "update_status", "target_id": "m1", "status": "stopped",
                                    "detail": {"effective": "last week"}}]}),
    ])
    llm = SafeLLM(fake)

    turn(store, "I take Advil.", "msg_1", llm)
    turn(store, "Actually I stopped last week.", "msg_2", llm)

    assert meds(store) == [("Advil", "stopped")]
    assert '"id": "m1"' in fake.prompts[1]["user"]           # the model saw an alias, not a UUID
    assert all(v.extracted_by == "llm" for v in store.all())


# ------------------------------------------------------- extraction coverage

def test_symptom_with_timeline_becomes_chief_complaint():
    store = InMemoryStore()
    turn(store, "I've had a headache for 3 days and some nausea", "msg_1")
    p = profile(store)
    assert p["chief_complaint"][0]["value"] == "headache"
    assert p["chief_complaint"][0]["detail"] == {"onset": "3 days"}
    assert p["symptom"][0]["value"] == "nausea"


def test_allergies_and_goal():
    store = InMemoryStore()
    turn(store, "I'm interested in egg freezing. I'm allergic to penicillin.", "msg_1")
    p = profile(store)
    assert p["goal"][0]["value"] == "egg freezing"
    assert "chief_complaint" not in p  # a service they want isn't a complaint
    assert p["allergy"][0]["value"] == "penicillin"


def test_symptom_resolves():
    store = InMemoryStore()
    turn(store, "I've had a headache since yesterday", "msg_1")
    turn(store, "my headache is gone now", "msg_2")
    assert profile(store)["chief_complaint"][0]["status"] == "resolved"


def test_value_correction_keeps_history():
    store = InMemoryStore()
    turn(store, "I take Advil", "msg_1")
    turn(store, "sorry, not Advil, it's Panadol", "msg_2")
    current = profile(store)["medication"]
    assert [(m["value"], m["versions"]) for m in current] == [("Panadol", 2)]


def test_ambiguous_correction_asks_instead_of_guessing():
    store = InMemoryStore()
    turn(store, "I take Advil and metformin", "msg_1")
    result = turn(store, "I stopped last week", "msg_2")
    assert result.clarify and "Advil" in result.clarify and "Metformin" in result.clarify
    assert sorted(meds(store)) == [("Advil", "active"), ("Metformin", "active")]


def test_retract():
    store = InMemoryStore()
    turn(store, "I take Advil", "msg_1")
    turn(store, "ignore that, I was thinking of my husband", "msg_2")
    assert "medication" not in profile(store)
    assert len(store.all()) == 2  # the retraction is a new row; nothing was deleted


# ----------------------------------------------------------------- safety

def test_no_known_allergies_conflict_is_flagged_not_resolved():
    store = InMemoryStore()
    turn(store, "I'm allergic to penicillin", "msg_1")
    turn(store, "I have no known allergies", "msg_2")
    allergies = profile(store)["allergy"]
    assert len(allergies) == 2  # both kept: a clinician decides
    flagged = next(a for a in allergies if a["value"] == "no known allergies")
    assert flagged["conflict_with_id"] == next(a for a in allergies if a["value"] == "penicillin")["id"]


def test_repeating_a_fact_does_not_duplicate_it():
    store = InMemoryStore()
    turn(store, "I take Advil", "msg_1")
    turn(store, "yes I take Advil", "msg_2")
    assert len(store.all()) == 1


@pytest.mark.parametrize("op, reason", [
    ({"op": "add", "kind": "medication", "value": "Advil", "status": "resolved"}, "not allowed"),
    ({"op": "add", "kind": "diagnosis", "value": "PCOS", "status": "active"}, "unknown kind"),
    ({"op": "add", "kind": "other", "value": "call 0123456789", "status": "active"}, "identifier"),
    ({"op": "add", "kind": "other", "value": "[REDACTED] is my doctor", "status": "active"}, "identifier"),
    ({"op": "update_status", "target_id": "nope", "status": "stopped"}, "unknown target"),
    ({"op": "drop_table"}, "unknown operation"),
])
def test_invalid_operations_are_rejected(op, reason):
    store = InMemoryStore()
    [result] = apply_operations(store, [op], "msg_1")
    assert not result.applied and reason in result.reason
    assert store.all() == []


def test_superseded_items_cannot_be_changed_again():
    store = InMemoryStore()
    [first] = apply_operations(store, [{"op": "add", "kind": "medication", "value": "Advil"}], "msg_1")
    apply_operations(store, [{"op": "update_status", "target_id": first.item.id, "status": "stopped"}], "msg_2")
    [stale] = apply_operations(store, [{"op": "update_status", "target_id": first.item.id, "status": "active"}], "msg_3")
    assert not stale.applied and "superseded" in stale.reason


def test_llm_junk_falls_back_to_rules():
    store = InMemoryStore()
    result = turn(store, "I take Advil", "msg_1", SafeLLM(FakeLLM(replies=["not json at all"])))
    assert result.source == "rules (llm unavailable)"
    assert meds(store) == [("Advil", "active")]


@pytest.mark.parametrize("clarify, shown", [
    ("question", None),                       # what OpenAI actually returned once
    ("null", None),
    ("", None),
    ("Which one did you stop, Advil or metformin?", "Which one did you stop, Advil or metformin?"),
])
def test_only_real_clarifying_questions_reach_the_patient(clarify, shown):
    reply = json.dumps({"operations": [], "clarify": clarify})
    assert extract("I stopped it", [], SafeLLM(FakeLLM(replies=[reply]))).clarify == shown


def test_llm_down_falls_back_to_rules():
    store = InMemoryStore()
    turn(store, "I take Advil", "msg_1", SafeLLM(FakeLLM(fail=True)))
    assert meds(store) == [("Advil", "active")]


def test_extraction_prompt_is_redacted():
    fake = FakeLLM(replies=['{"operations": []}'])
    extract("My name is John Doe and I take Advil", [], SafeLLM(fake))
    assert "John Doe" not in fake.last_prompt and "Advil" in fake.last_prompt


@pytest.mark.parametrize("text", [
    "I'm on my period", "I am on holiday next week", "I use an app to track my cycle",
    "I'm taking a break from work", "I take care of my mum",
])
def test_everyday_phrases_are_not_medications(text):
    assert rule_extract(text, []).operations == []


def test_known_medication_keeps_its_duration():
    [op] = rule_extract("I've been on Clomid for 2 months", []).operations
    assert (op["value"], op["detail"]) == ("Clomid", {"since": "2 months"})


@pytest.mark.parametrize("text, symptoms", [
    ("really bloated since the egg retrieval and I feel dizzy", {"bloating", "dizziness"}),
    ("I'm pregnant and bleeding heavily with cramps", {"bleeding", "cramps"}),
])
def test_everyday_symptom_words_are_recognised(text, symptoms):
    found = {op["value"] for op in rule_extract(text, []).operations if op["kind"] in ("symptom", "chief_complaint")}
    assert symptoms <= found


def test_a_duration_attaches_only_to_what_it_describes():
    ops = rule_extract("I've had irregular periods for 6 months, I take metformin", []).operations
    by_value = {op["value"]: op for op in ops}
    assert by_value["irregular periods"]["detail"] == {"onset": "6 months"}
    assert by_value["Metformin"]["detail"] == {}


def test_stopping_an_unknown_medication_does_not_add_it_as_active():
    ops = rule_extract("I stopped Advil a month ago", []).operations
    assert not any(op.get("status") == "active" and op.get("kind") == "medication" for op in ops)
