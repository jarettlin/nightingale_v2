"""Turn a patient message into proposed memory operations.

Two extractors, same output shape:
  * LLM extractor: handles free phrasing; its JSON is validated before use.
  * Rule extractor: small lexicons and patterns; works when the LLM is down,
    the same fallback idea as the risk gate.

Neither writes anything. `memory.apply_operations` decides what is stored.
When a correction is ambiguous ("I stopped it" with two active medications),
we return a clarifying question instead of guessing.
"""

import json
import re
from dataclasses import dataclass, field

from app.llm import LLMUnavailable, SafeLLM
from app.memory import ALLOWED_STATUS, NO_KNOWN_ALLERGIES, MemoryItem
from app.redaction import RedactionError


@dataclass
class Extraction:
    operations: list[dict] = field(default_factory=list)
    clarify: str | None = None   # a question to ask instead of guessing
    source: str = "rules"        # rules | llm | rules (llm unavailable)


# --------------------------------------------------------------------- rules

MEDICATIONS = [
    "advil", "ibuprofen", "panadol", "paracetamol", "acetaminophen", "aspirin", "naproxen",
    "metformin", "clomid", "clomiphene", "letrozole", "progesterone", "estradiol", "estrogen",
    "folic acid", "vitamin d", "prenatal vitamins", "gonal-f", "menopur", "ovidrel", "cetrotide",
    "birth control", "the pill", "levothyroxine", "insulin", "prednisolone",
]
SYMPTOMS = [
    "headache", "migraine", "nausea", "vomiting", "cramps", "cramping", "bloating", "spotting",
    "fatigue", "tiredness", "dizziness", "hot flashes", "back pain", "pelvic pain", "abdominal pain",
    "irregular periods", "missed period", "heavy periods", "painful periods", "acne", "hair loss",
    "insomnia", "anxiety", "low mood", "breast tenderness",
]
GOALS = {
    "egg freezing": "egg freezing", "freeze my eggs": "egg freezing", "ivf": "IVF",
    "trying to conceive": "trying to conceive", "get pregnant": "trying to conceive",
    "fertility test": "fertility testing", "amh test": "fertility testing", "iui": "IUI",
}

TIME = r"(yesterday|today|this morning|last (?:night|week|month|year)|(?:\d+|a few|a couple of|two|three) (?:days?|weeks?|months?|years?) ago)"
DURATION = r"(?:for|since) ((?:the (?:past|last) )?(?:\d+|a few|a couple of|two|three|several) (?:days?|weeks?|months?|years?)|yesterday|last (?:week|month|year))"

STOP_CUES = re.compile(r"\b(stopped|quit|no longer (?:take|taking|on)|not taking|came off|went off|don'?t take)\b", re.I)
RESOLVED_CUES = re.compile(r"\b(is|are|has|have)\s+(gone|better|stopped|resolved|cleared up)\b|\bno longer have\b", re.I)
RETRACT_CUES = re.compile(r"\b(ignore that|scratch that|i was wrong about|never mind about)\b", re.I)
CORRECTION = re.compile(r"\b(?:not|isn'?t|wasn'?t)\s+(?P<old>[\w -]+?),?\s+(?:it'?s|i (?:meant|mean))\s+(?P<new>[\w -]+)", re.I)
NO_ALLERGIES = re.compile(r"\b(no (?:known )?allergies|not allergic to anything|don'?t have any allergies)\b", re.I)
ALLERGY = re.compile(r"\ballergic to (?P<x>[a-z][a-z -]{1,40}?)(?=[.,!?]|\band\b|$)", re.I)


def _find_terms(text: str, terms) -> list[str]:
    low = text.lower()
    return [t for t in terms if re.search(rf"\b{re.escape(t)}\b", low)]


def _current(profile_items: list[MemoryItem], kind: str, status: str = "active") -> list[MemoryItem]:
    return [i for i in profile_items if i.kind == kind and i.status == status]


def _target(items: list[MemoryItem], named: list[str]) -> tuple[MemoryItem | None, bool]:
    """Pick the item a correction refers to. Returns (item, ambiguous)."""
    if named:
        hits = [i for i in items if i.value.lower() in named]
        return (hits[0], False) if hits else (None, False)
    if len(items) == 1:
        return items[0], False
    return None, len(items) > 1


def rule_extract(text: str, current: list[MemoryItem]) -> Extraction:
    ops: list[dict] = []
    clarify = None
    meds_named = _find_terms(text, MEDICATIONS)
    symptoms_named = _find_terms(text, SYMPTOMS)
    when = re.search(TIME, text, re.I)
    since = re.search(DURATION, text, re.I)

    # Value corrections: "not Advil, it's Panadol"
    if m := CORRECTION.search(text):
        old = m.group("old").strip().lower()
        target = next((i for i in current if i.value.lower() == old), None)
        if target:
            ops.append({"op": "correct_value", "target_id": target.id, "value": m.group("new").strip()})
            return Extraction(ops)

    if RETRACT_CUES.search(text):
        target, ambiguous = _target([i for i in current if i.status != "retracted"],
                                    meds_named + symptoms_named)
        if target:
            ops.append({"op": "retract", "target_id": target.id})
        elif ambiguous:
            clarify = "Sorry, which part should I ignore?"
        return Extraction(ops, clarify)

    # Stopping a medication: "Actually I stopped last week"
    if STOP_CUES.search(text):
        target, ambiguous = _target(_current(current, "medication"), meds_named)
        if target:
            detail = {"effective": when.group(1)} if when else {}
            ops.append({"op": "update_status", "target_id": target.id, "status": "stopped", "detail": detail})
            meds_named = [m for m in meds_named if m != target.value.lower()]
        elif ambiguous:
            names = ", ".join(i.value for i in _current(current, "medication"))
            clarify = f"Just to make sure I get this right: which one did you stop ({names})?"
        else:
            meds_named = []  # "I stopped Advil" with no Advil on file: don't add it as active

    # Symptoms resolving: "my headache is gone"
    if RESOLVED_CUES.search(text):
        target, ambiguous = _target(_current(current, "symptom") + _current(current, "chief_complaint"),
                                    symptoms_named)
        if target:
            ops.append({"op": "update_status", "target_id": target.id, "status": "resolved",
                        "detail": {"effective": when.group(1)} if when else {}})
            symptoms_named = [s for s in symptoms_named if s != target.value.lower()]
        elif ambiguous:
            clarify = clarify or "Which symptom has improved?"

    # New medications: known names only. Free text after "I'm on…" turns
    # "my period" or "holiday" into drugs; unfamiliar drug names are the LLM's job.
    for med in meds_named:
        detail = {"since": since.group(1)} if since else {}
        ops.append({"op": "add", "kind": "medication", "value": med.title(), "status": "active", "detail": detail})

    # Allergies
    if NO_ALLERGIES.search(text):
        ops.append({"op": "add", "kind": "allergy", "value": NO_KNOWN_ALLERGIES, "status": "active"})
    for m in ALLERGY.finditer(text):
        ops.append({"op": "add", "kind": "allergy", "value": m.group("x").strip().lower(), "status": "active"})

    # Symptoms; the first one we hear becomes the chief complaint. Services they
    # ask about ("how much is egg freezing?") are goals, never complaints.
    has_chief = bool(_current(current, "chief_complaint"))
    for i, symptom in enumerate(symptoms_named):
        detail = {"onset": since.group(1)} if since else {}
        kind = "symptom" if has_chief or i > 0 else "chief_complaint"
        ops.append({"op": "add", "kind": kind, "value": symptom, "status": "active", "detail": detail})
        has_chief = True

    for phrase, goal in GOALS.items():
        if re.search(rf"\b{re.escape(phrase)}\b", text, re.I):
            ops.append({"op": "add", "kind": "goal", "value": goal, "status": "active"})
            break

    return Extraction(ops, clarify)


# ----------------------------------------------------------------------- LLM

EXTRACT_SYSTEM = (
    "You maintain a patient's structured profile for a clinic. You never diagnose. "
    "Given CURRENT_PROFILE and a new MESSAGE, return JSON only:\n"
    '{"operations": [...], "clarify": null or "question"}\n'
    "Operations:\n"
    '  {"op":"add","kind":K,"value":V,"status":S,"detail":{...}}\n'
    '  {"op":"update_status","target_id":ID,"status":S,"detail":{"effective":"last week"}}\n'
    '  {"op":"correct_value","target_id":ID,"value":V}\n'
    '  {"op":"retract","target_id":ID}\n'
    "Kinds and statuses: " + json.dumps({k: sorted(v) for k, v in ALLOWED_STATUS.items()}) + "\n"
    "Rules: only record what the patient states about themselves. Use update_status when they "
    "correct an existing item; never re-add it. Keep onset/duration in detail. If a correction is "
    "ambiguous, return no operation and ask in clarify. Never put names or identifiers in values; "
    "[REDACTED] marks removed identifiers."
)


def _profile_for_prompt(current: list[MemoryItem]) -> tuple[str, dict[str, str]]:
    """Short aliases (m1, m2…) instead of UUIDs: fewer tokens, and a UUID's digit
    runs can look like phone numbers to the redactor."""
    items = [i for i in current if i.status != "retracted"]
    aliases = {f"m{n}": i.id for n, i in enumerate(items, 1)}
    rows = [{"id": alias, "kind": i.kind, "value": i.value, "status": i.status}
            for alias, i in zip(aliases, items)]
    return json.dumps(rows), aliases


def _parse(raw: str, aliases: dict[str, str]) -> Extraction:
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("operations", []), list):
        raise ValueError("bad shape")
    ops = []
    for op in data.get("operations", []):
        if isinstance(op, dict):
            if "target_id" in op:  # unknown aliases stay unknown and get rejected later
                op = {**op, "target_id": aliases.get(op["target_id"], op["target_id"])}
            ops.append(op)
    clarify = data.get("clarify")
    return Extraction(ops, clarify if isinstance(clarify, str) and clarify else None, "llm")


def extract(text: str, current: list[MemoryItem], llm: SafeLLM | None = None) -> Extraction:
    """LLM first when available; rules when it's missing, down, or returns junk."""
    if llm is not None:
        profile_json, aliases = _profile_for_prompt(current)
        prompt = f"CURRENT_PROFILE: {profile_json}\nMESSAGE: {text}"
        try:
            return _parse(llm.complete(EXTRACT_SYSTEM, prompt, purpose="extract"), aliases)
        except (LLMUnavailable, RedactionError, ValueError):
            result = rule_extract(text, current)
            result.source = "rules (llm unavailable)"
            return result
    return rule_extract(text, current)
