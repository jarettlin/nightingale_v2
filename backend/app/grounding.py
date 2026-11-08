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
"""

import re
from dataclasses import dataclass, field

from app.knowledge import Chunk, tokens

# [S1], and the grouped forms models also produce: [S2,S3], [S2, S3]
MARKER = re.compile(r"\s*\[(S\d+(?:\s*,\s*S?\d+)*)\]")
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
CONVERSATIONAL = re.compile(
    r"^(thank|thanks|glad|happy to help|i hope|hope this|feel free|let me know|if you('d| would) like|"
    r"you can (tap|send|ask|continue|book)|is there anything|please|great question|good question)", re.I)
MIN_OVERLAP = 0.4


@dataclass
class GroundedReply:
    text: str
    citations: list[tuple[str, Chunk]] = field(default_factory=list)  # (marker, chunk) actually used
    dropped: int = 0
    grounded: bool = False

    @property
    def confidence(self) -> str:
        if not self.grounded:
            return "low"
        return "high" if self.dropped == 0 else "med"


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
    kept, used, dropped, cited_kept = [], {}, 0, 0

    for sentence in _sentences(reply):
        markers = _marker_numbers(sentence)
        # "next steps, [S2,S4]." minus its marker would leave "next steps,."
        claim = re.sub(r"\s*,\s*([.!?])", r"\1", MARKER.sub("", sentence)).strip()
        valid = [m for m in dict.fromkeys(markers) if m in by_marker]
        if markers:
            if valid and _supported(claim, [by_marker[m] for m in valid], patient_numbers):
                kept.append(claim + "".join(f" [S{m}]" for m in valid))
                used.update({m: by_marker[m] for m in valid})
                cited_kept += 1
            else:
                dropped += 1
        elif CONVERSATIONAL.match(claim) and not _numbers(claim):
            kept.append(claim)
        elif any(_supported(claim, [c], patient_numbers) for c in sources):
            kept.append(claim)
        else:
            dropped += 1

    return GroundedReply(text=" ".join(kept), citations=[(f"S{m}", c) for m, c in used.items()],
                         dropped=dropped, grounded=cited_kept > 0)
