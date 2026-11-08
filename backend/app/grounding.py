"""Check a generated reply against the sources it was given. We trust the model
to phrase things, not to be right about the clinic.

Each sentence of the reply is kept only if it is supported:
  * its [S#] markers must refer to sources we actually sent (fake ones removed);
    models also write [S2,S3], which counts as two markers
  * every number in it (prices, days, hours) must appear in a cited source,
    or in what the patient said ("since you're 38" echoes them, it isn't a claim)
  * enough of its content words must appear in the cited source(s)
  * a sentence with no marker survives only if it's plainly conversational
    ("Happy to help!") or clearly drawn from one of the sources
If no cited sentence survives, the reply is not grounded and the caller
replaces it with an honest "I don't have that information".

The rules catch invented numbers, fake citations and flipped negations, but a
reworded outside fact ("egg freezing is covered by insurance") can share
enough words with a source to pass. So `verify_claims` then asks the model,
in a separate strict JSON call, whether each remaining sentence is actually
stated by its sources, and drops the ones that aren't. It can only remove
sentences, never add or change them.
"""

import json
import re
from dataclasses import dataclass, field, replace

from app.knowledge import Chunk, tokens
from app.llm import LLMUnavailable, SafeLLM
from app.logs import log_event
from app.redaction import RedactionError

# [S1], and the grouped forms models also produce: [S2,S3], [S2, S3]
MARKER = re.compile(r"\s*\[(S\d+(?:\s*,\s*S?\d+)*)\]")
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
CONVERSATIONAL = re.compile(
    r"^(thank|thanks|glad|happy to help|i hope|hope this|feel free|let me know|if you('d| would) like|"
    r"you can (tap|send|ask|continue|book)|is there anything|please|great question|good question)", re.I)
MIN_OVERLAP = 0.4


@dataclass
class KeptSentence:
    text: str                       # as shown, markers included
    claim: str                      # without markers
    markers: list[str]              # e.g. ["1", "3"]
    conversational: bool = False


@dataclass
class GroundedReply:
    text: str
    citations: list[tuple[str, Chunk]] = field(default_factory=list)  # (marker, chunk) actually used
    dropped: int = 0
    grounded: bool = False
    sentences: list[KeptSentence] = field(default_factory=list)
    verified: bool | None = None    # None: not checked; False: checker unavailable; True: checked

    @property
    def confidence(self) -> str:
        if not self.grounded:
            return "low"
        # An unverified reply passed the rules only, so it can't be "high"
        return "high" if self.dropped == 0 and self.verified is not False else "med"


LEADING_MARKERS = re.compile(r"^((?:\s*\[S\d+(?:\s*,\s*S?\d+)*\])+)\s*")


def _marker_numbers(sentence: str) -> list[str]:
    """'…weeks. [S1] [S2,S3]' -> ['1', '2', '3']"""
    return [n for group in MARKER.findall(sentence) for n in re.findall(r"\d+", group)]


def _sentences(reply: str) -> list[str]:
    """Split into sentences. Models write "…IUI. [S1] We do not…": markers right
    after a full stop belong to the sentence before, not the one after."""
    merged: list[str] = []
    for part in re.split(r"(?<=[.!?])\s+", reply.strip()):
        lead = LEADING_MARKERS.match(part)
        if lead and merged:
            merged[-1] += " " + lead.group(1).strip()
            part = part[lead.end():]
        if part.strip():
            merged.append(part.strip())
    return merged


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in NUMBER.findall(text)}


NEGATION = re.compile(r"\b(not|no|never|don'?t|doesn'?t|isn'?t|aren'?t|cannot|can'?t|won'?t|without)\b", re.I)
CLAUSE_BREAK = re.compile(r",?\s+but\s+|;\s*|,\s+(?:while|although|whereas)\s+", re.I)
# Words that appear on both sides of a "we do / we don't" and so can't tell them apart
GENERIC = set(tokens("we our us you offer offers service services provide clinic sunrise fertility can"))


def _negated(text: str) -> bool:
    return bool(NEGATION.search(text))


def _pairs(words: list[str]) -> set[tuple[str, str]]:
    return set(zip(words, words[1:]))


def _polarity_ok(clause: str, source_sentences: list[str]) -> bool:
    """The clause must agree on "not" with the source sentences that best match it.
    Word pairs count double, so "egg freezing" and "sperm freezing" stay distinct."""
    words = [w for w in tokens(clause) if w not in GENERIC]
    if not words:
        return True
    unigrams, pairs = set(words), _pairs(words)

    def score(sentence: str) -> int:
        s_words = [w for w in tokens(sentence) if w not in GENERIC]
        return len(unigrams & set(s_words)) + 2 * len(pairs & _pairs(s_words))

    same = max((score(s) for s in source_sentences if _negated(s) == _negated(clause)), default=0)
    opposite = max((score(s) for s in source_sentences if _negated(s) != _negated(clause)), default=0)
    return same > 0 and same >= opposite


def _supported(claim: str, chunks: list[Chunk], patient_numbers: frozenset[str] = frozenset()) -> bool:
    if not chunks:
        return False
    # A price or a duration the sources don't contain is invented, however fluent.
    # Numbers the patient gave (their age, how long) may be echoed back.
    source_numbers = set().union(*(_numbers(c.text) for c in chunks)) | patient_numbers
    if not _numbers(claim) <= source_numbers:
        return False
    words = set(tokens(claim))
    if not words:
        return False
    source_words = set().union(*(set(tokens(f"{c.heading} {c.text}")) for c in chunks))
    overlap = len(words & source_words)
    if overlap < 2 or overlap / len(words) < MIN_OVERLAP:
        return False
    # Same words, opposite meaning: "We offer sperm freezing" vs "We do not offer
    # sperm freezing". Checked per clause, because one sentence often joins a
    # "we don't" and a "we can": "We don't offer X, but we can refer you".
    source_sentences = [s for c in chunks for s in re.split(r"(?<=[.!?])\s+", c.text)]
    return all(_polarity_ok(clause, source_sentences) for clause in CLAUSE_BREAK.split(claim) if clause.strip())


def check_reply(reply: str, sources: list[Chunk], patient_text: str = "") -> GroundedReply:
    by_marker = {str(n): chunk for n, chunk in enumerate(sources, 1)}
    patient_numbers = frozenset(_numbers(patient_text))
    kept: list[KeptSentence] = []
    dropped = 0

    for sentence in _sentences(reply):
        markers = _marker_numbers(sentence)
        # "next steps, [S2,S4]." minus its marker would leave "next steps,."
        claim = re.sub(r"\s*,\s*([.!?])", r"\1", MARKER.sub("", sentence)).strip()
        valid = [m for m in dict.fromkeys(markers) if m in by_marker]
        if markers:
            if valid and _supported(claim, [by_marker[m] for m in valid], patient_numbers):
                kept.append(KeptSentence(claim + "".join(f" [S{m}]" for m in valid), claim, valid))
            else:
                dropped += 1
        elif CONVERSATIONAL.match(claim) and not _numbers(claim):
            kept.append(KeptSentence(claim, claim, [], conversational=True))
        elif any(_supported(claim, [c], patient_numbers) for c in sources):
            kept.append(KeptSentence(claim, claim, []))
        else:
            dropped += 1

    return _assemble(kept, by_marker, dropped)


def _assemble(kept: list[KeptSentence], by_marker: dict[str, Chunk], dropped: int,
              verified: bool | None = None) -> GroundedReply:
    used = {m: by_marker[m] for s in kept for m in s.markers}
    return GroundedReply(text=" ".join(s.text for s in kept), citations=[(f"S{m}", c) for m, c in used.items()],
                         dropped=dropped, grounded=any(s.markers for s in kept), sentences=kept,
                         verified=verified)


# ------------------------------------------------------------------ model claim check

VERIFY_SYSTEM = """You check a fertility clinic assistant's reply against the clinic's own source texts.

For each numbered statement, decide if it is SUPPORTED: everything it says is stated in the sources,
or it only repeats what the patient said. Rewording and summarising are fine.

It is NOT supported if it adds anything the sources don't state: a fact, number, service, price,
promise, timeframe or medical advice, even if it is true in general. Also not supported if it
changes the meaning: "usually" becomes "always", a condition or a "not" is dropped, or a general
fact is turned into a prediction for this patient. Use only the sources, never outside knowledge.

Return only JSON: {"results": [{"n": 1, "supported": true}, {"n": 2, "supported": false}]}"""


def _verify_input(sentences: list[KeptSentence], sources: list[Chunk], patient_text: str) -> str:
    src = "\n\n".join(f"[S{n}] ({c.heading})\n{c.text}" for n, c in enumerate(sources, 1))
    stmts = "\n".join(f"{n}. {s.claim}" for n, s in enumerate(sentences, 1))
    return f"SOURCES:\n{src}\n\nPATIENT SAID:\n{patient_text}\n\nSTATEMENTS:\n{stmts}"


def verify_claims(checked: GroundedReply, sources: list[Chunk], llm: SafeLLM, patient_text: str = "",
                  known_identifiers: list[str] | None = None) -> GroundedReply:
    """Second opinion on every factual sentence that passed the rules. Only removes.

    If the checker is unavailable or answers junk, the rule-checked reply stands but
    its confidence is capped at "med": the rules already removed what they can see,
    and one slow call shouldn't turn every answer into "I'd rather not guess"."""
    by_marker = {str(n): chunk for n, chunk in enumerate(sources, 1)}
    to_check = [s for s in checked.sentences if not s.conversational]
    if not checked.grounded or not to_check:
        return checked
    try:
        raw = llm.complete(VERIFY_SYSTEM, _verify_input(to_check, sources, patient_text), purpose="verify",
                           known_identifiers=known_identifiers)
        results = json.loads(raw)["results"]
        verdict = {int(r["n"]): r.get("supported") is True for r in results}
    except (LLMUnavailable, RedactionError, ValueError, KeyError, TypeError):
        log_event("claims_unverified")
        return replace(checked, verified=False)

    # A statement the checker didn't answer for counts as unsupported
    rejected = {id(s) for n, s in enumerate(to_check, 1) if not verdict.get(n, False)}
    kept = [s for s in checked.sentences if id(s) not in rejected]
    log_event("claims_verified", checked=len(to_check), rejected=len(rejected))
    return _assemble(kept, by_marker, checked.dropped + len(rejected), verified=True)
