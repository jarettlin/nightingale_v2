"""PHI redaction: the only door between patient text and the LLM.

We de-identify, we don't de-medicalise. Identifiers (names, IC/NRIC/passport
numbers, phones, emails, handles, full dates) are replaced with [REDACTED].
Clinical content (symptoms, durations, medications) is kept, because without
it the LLM is useless.

Layers, cheapest and most reliable first:
  1. Regex for structured identifiers (IC, NRIC, passport, phone, email, handle, date)
  2. Name cues ("my name is ...", "Mr ...", "Ahmad bin Ali")
  3. Known identifiers for this person (their stored email/phone/handle/name)

Then a verification pass re-scans the output. If anything identifier-shaped
survives, or anything throws, the result is not ok and the caller must not
send it to the LLM (fail closed).
"""

import re
from dataclasses import dataclass, field

from app.logs import log_event

PLACEHOLDER = "[REDACTED]"

# Order matters only for readability; overlaps are merged afterwards.
ID_PATTERNS: dict[str, re.Pattern] = {
    "EMAIL": re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    # Malaysian MyKad: 900101-14-5678 or 900101145678
    "IC": re.compile(r"\b\d{6}-?\d{2}-?\d{4}\b"),
    # Singapore NRIC/FIN: S1234567A
    "NRIC": re.compile(r"\b[STFGM]\d{7}[A-Z]\b", re.IGNORECASE),
    # Passport, e.g. Malaysian A12345678
    "PASSPORT": re.compile(r"\b[A-Z]\d{8}\b"),
    # Full dates with a year are identifiers (DOB); "last week" is not
    "DATE": re.compile(r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b"),
    # Social handle: @sarah.t (emails are matched first and win the overlap)
    "HANDLE": re.compile(r"(?<![\w.])@[A-Za-z0-9_][A-Za-z0-9_.]{1,29}"),
    # Phone candidates: digits with separators; filtered by digit count below
    "PHONE": re.compile(r"(?<![\w])\+?\d[\d\s().-]{6,}\d(?![\w])"),
}

_NAME_WORD = r"[A-Z][a-z'’-]+"
_NAME_SEQ = rf"{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,3}}"

NAME_PATTERNS: list[re.Pattern] = [
    # "my name is john doe" — after this cue, accept any case
    re.compile(r"\b(?:my name is|my name's|name:)\s+([A-Za-z'’-]+(?:\s+(?!and\b|i\b|my\b)[A-Za-z'’-]+){0,2})", re.IGNORECASE),
    # "I'm Sarah", "call me Sarah" — require a capitalised word
    re.compile(rf"\b(?:[Ii]'?m|[Ii] am|[Cc]all me|[Tt]his is)\s+({_NAME_SEQ})"),
    # Honorifics, including Malay ones
    re.compile(rf"\b(?:Mr|Mrs|Ms|Mdm|Madam|Dr|Encik|Cik|Puan|Tuan|Dato'?|Datin)\.?\s+({_NAME_SEQ})"),
    # Patronymics: "Ahmad bin Ali", "Siti binti Hassan", "Raj a/l Kumar"
    re.compile(rf"\b({_NAME_SEQ}\s+(?:bin|binti|bte|a/l|a/p|s/o|d/o)\s+{_NAME_SEQ})"),
]

# Capitalised words that follow "I'm" but aren't names
NOT_NAMES = {
    "Worried", "Scared", "Not", "Feeling", "Pregnant", "Sorry", "Fine", "Okay", "Ok",
    "Trying", "Having", "Taking", "Here", "Just", "Still", "So", "Very", "Really",
    "Thinking", "Wondering", "Interested", "Asking", "Looking", "Over", "Under",
}


@dataclass
class Span:
    start: int
    end: int
    kind: str


@dataclass
class RedactionResult:
    redacted_text: str
    entities: list[dict] = field(default_factory=list)  # kinds and positions only, never values
    ok: bool = True
    error: str | None = None


class RedactionError(Exception):
    """Raised when text cannot be made safe. Callers must not reach the LLM."""


def _digits(s: str) -> int:
    return sum(c.isdigit() for c in s)


def _find_id_spans(text: str) -> list[Span]:
    spans = []
    for kind, pattern in ID_PATTERNS.items():
        for m in pattern.finditer(text):
            if kind == "PHONE" and not 8 <= _digits(m.group()) <= 15:
                continue
            spans.append(Span(m.start(), m.end(), kind))
    return spans


def _find_name_spans(text: str) -> list[Span]:
    spans = []
    for pattern in NAME_PATTERNS:
        for m in pattern.finditer(text):
            name = m.group(1).strip()
            if name.split()[0] in NOT_NAMES:
                continue
            start = m.start(1)
            spans.append(Span(start, start + len(name), "NAME"))
    return spans


def _find_known_spans(text: str, known: list[str]) -> list[Span]:
    spans = []
    for value in known:
        if value and len(value) >= 3:
            for m in re.finditer(re.escape(value), text, re.IGNORECASE):
                spans.append(Span(m.start(), m.end(), "KNOWN"))
    return spans


def _merge(spans: list[Span]) -> list[Span]:
    """Merge overlapping spans; the earliest-starting span's kind wins."""
    merged: list[Span] = []
    for s in sorted(spans, key=lambda s: (s.start, -(s.end - s.start))):
        if merged and s.start < merged[-1].end:
            merged[-1].end = max(merged[-1].end, s.end)
        else:
            merged.append(Span(s.start, s.end, s.kind))
    return merged


def _residual_identifiers(text: str) -> list[str]:
    """Re-scan redacted output. Names aren't re-checked (placeholders break the cues)."""
    return [s.kind for s in _find_id_spans(text.replace(PLACEHOLDER, " "))]


def redact(text: str, known_identifiers: list[str] | None = None) -> RedactionResult:
    try:
        spans = _merge(
            _find_id_spans(text)
            + _find_name_spans(text)
            + _find_known_spans(text, known_identifiers or [])
        )
        out, cursor = [], 0
        for s in spans:
            out.append(text[cursor:s.start])
            out.append(PLACEHOLDER)
            cursor = s.end
        out.append(text[cursor:])
        redacted = "".join(out)

        residual = _residual_identifiers(redacted)
        if residual:
            log_event("redaction_failed", reason="residual_identifiers", kinds=residual)
            return RedactionResult(redacted, ok=False, error="residual_identifiers")

        entities = [{"kind": s.kind, "start": s.start, "end": s.end} for s in spans]
        log_event("redaction_applied", entity_count=len(entities),
                  kinds=sorted({e["kind"] for e in entities}))
        return RedactionResult(redacted, entities)
    except Exception as exc:  # fail closed: never hand back the original text
        log_event("redaction_failed", reason="exception", error_type=type(exc).__name__)
        return RedactionResult("", ok=False, error="exception")


def redact_or_raise(text: str, known_identifiers: list[str] | None = None) -> str:
    result = redact(text, known_identifiers)
    if not result.ok:
        raise RedactionError(result.error)
    return result.redacted_text
