"""Risk gate: runs on every patient message before any reply is generated.

Two opinions, one rule:
  1. Deterministic rules (this file). They can't be argued with, and they work
     when the LLM is down. "crushing chest pain" is High every time.
  2. An optional LLM opinion, for phrasing the rules don't know.
  Final risk = the HIGHER of the two. The LLM can raise risk, never lower it.

Negation ("no chest pain") lowers a High match to Medium, never to Low:
we'd rather ask a clinician than miss a misread sentence.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum


class Risk(IntEnum):
    LOW = 1
    MED = 2
    HIGH = 3

    @property
    def label(self) -> str:
        return {1: "low", 2: "med", 3: "high"}[self.value]


@dataclass(frozen=True)
class Rule:
    code: str      # logged instead of the matched text
    reason: str    # shown to clinicians
    pattern: re.Pattern


def _rule(code: str, reason: str, *phrases: str) -> Rule:
    return Rule(code, reason, re.compile(r"\b(?:" + "|".join(phrases) + r")", re.IGNORECASE))


HIGH_RULES = [
    _rule("cardiac", "possible cardiac emergency",
          r"(crushing|severe|sharp|sudden|bad)\s+(chest|heart)\s+(pain|pressure)",
          r"chest\s+(pain|pressure|tightness)", r"pain in (my|the) chest",
          r"heart attack", r"chest (is|feels) (tight|crushed|heavy)"),
    _rule("breathing", "breathing difficulty",
          r"(difficulty|trouble|struggling|hard)\s+(to\s+)?breath(e|ing)", r"can'?t breathe",
          r"cannot breathe", r"short(ness)? of breath", r"breathless", r"choking"),
    _rule("bleeding", "heavy or uncontrolled bleeding",
          r"heavy bleeding", r"bleeding (heavily|a lot|non ?stop)", r"(won'?t|will not|can'?t) stop bleeding",
          r"soak(ing|ed) (through )?(a |my )?pads?", r"(vomit|cough)(ing|ed)? (up )?blood"),
    _rule("self_harm", "risk of self-harm",
          r"(want|going|thinking about|thoughts of) (to )?(hurt|harm|kill)(ing)? myself", r"hurt myself",
          r"kill myself", r"end(ing)? (it all|my life)", r"suicid\w*", r"don'?t want to (live|be alive)",
          r"self[- ]?harm", r"overdos\w*", r"cut(ting)? myself"),
    _rule("stroke", "possible stroke signs",
          r"face (is )?droop\w*", r"slurr\w* speech", r"can'?t (move|feel) (my )?(left|right|one) (side|arm|leg)",
          r"sudden (numbness|weakness)"),
    _rule("neuro", "possible neurological emergency",
          r"worst headache", r"seizure", r"(passed out|fainted|unconscious|blacked out)"),
    _rule("anaphylaxis", "possible severe allergic reaction",
          r"(throat|tongue|lips?) (is |are )?(swell|swollen|closing)\w*", r"anaphyla\w*"),
    _rule("abdominal", "severe abdominal pain (OHSS / ectopic must be ruled out)",
          r"severe (abdominal|stomach|tummy|pelvic) pain", r"(bleeding|pain) (while|and) pregnant"),
]

MED_RULES = [
    _rule("chest_ambiguous", "ambiguous chest symptom",
          r"chest (feels|is|has been) (funny|weird|strange|off|odd)", r"palpitations?",
          r"heart (is )?(racing|pounding|skipping)"),
    _rule("neuro_ambiguous", "ambiguous neurological symptom",
          r"dizz\w*", r"light ?headed", r"numb\w*", r"tingl\w*", r"blurr\w* vision", r"faint"),
    _rule("bleeding_other", "bleeding that needs clinical context", r"bleed\w*", r"spotting", r"blood in"),
    _rule("fever", "fever", r"fever", r"high temperature"),
    _rule("pain_severe", "significant pain", r"(severe|unbearable|excruciating|really bad) pain",
          r"pain (is )?(getting worse|worsening)"),
    _rule("bloating_post_procedure", "symptom after a procedure (possible OHSS)",
          r"(bloat\w*|swollen|weight gain).{0,40}(retrieval|stimulation|injections?|transfer)",
          r"(retrieval|stimulation|injections?|transfer).{0,40}(bloat\w*|swollen|weight gain)"),
    _rule("lump", "new lump", r"\blump\b"),
]

# Not risk: what the patient is asking for. These still route to a clinician.
INTENT_RULES = {
    "wants_diagnosis": re.compile(r"\b(do i have|is (it|this) (cancer|serious|normal)|what'?s wrong with me|"
                                  r"diagnos\w*|what do i have|am i (sick|infertile))", re.IGNORECASE),
    "medication_change": re.compile(r"\b(should i|can i|is it ok(ay)? to) (stop|start|take|increase|double|"
                                    r"reduce|skip|change|switch)\b", re.IGNORECASE),
    "uncertain": re.compile(r"\b(not sure|unsure|confused|don'?t (know|understand)|is that bad|should i worry)\b",
                            re.IGNORECASE),
}

NEGATION = re.compile(r"\b(no|not|never|don'?t have|do not have|without|denies|haven'?t had|no longer)\b[\w\s,]{0,20}$",
                      re.IGNORECASE)


@dataclass
class RiskAssessment:
    risk_level: Risk
    risk_reason: str
    confidence: str                 # low / med / high: how sure we are of this assessment
    risk_provenance: str            # ISO timestamp of the assessment
    source: str                     # rules | rules+llm | rules+llm_unavailable
    rule_codes: list[str] = field(default_factory=list)
    intents: list[str] = field(default_factory=list)

    @property
    def escalation_required(self) -> bool:
        return (self.risk_level >= Risk.MED
                or "wants_diagnosis" in self.intents
                or "medication_change" in self.intents)

    @property
    def escalation_offered(self) -> bool:
        return self.escalation_required or "uncertain" in self.intents


def _match(rules: list[Rule], text: str) -> list[tuple[Rule, bool]]:
    """Return (rule, negated) for each rule that matches."""
    hits = []
    for rule in rules:
        m = rule.pattern.search(text)
        if m:
            negated = bool(NEGATION.search(text[:m.start()][-30:]))
            hits.append((rule, negated))
    return hits


def rule_assessment(text: str) -> tuple[Risk, str, list[str]]:
    high = _match(HIGH_RULES, text)
    if any(not neg for _, neg in high):
        hits = [r for r, neg in high if not neg]
        return Risk.HIGH, "; ".join(r.reason for r in hits), [r.code for r in hits]

    med = _match(MED_RULES, text)
    if high or med:
        reasons = [f"{r.reason} (mentioned as absent, confirming)" for r, _ in high]
        reasons += [r.reason for r, _ in med]
        return Risk.MED, "; ".join(reasons), [r.code for r, _ in high + med]

    return Risk.LOW, "no risk indicators matched", []


def assess(text: str, llm_opinion: dict | None = None, llm_unavailable: bool = False) -> RiskAssessment:
    """`llm_opinion` is the parsed JSON {"risk_level": "low|med|high", "reason": str, "confidence": str}."""
    level, reason, codes = rule_assessment(text)
    intents = [name for name, pattern in INTENT_RULES.items() if pattern.search(text)]
    confidence = "high" if level > Risk.LOW else "med"
    source = "rules"

    if llm_unavailable:
        source = "rules+llm_unavailable"
        if level < Risk.MED:
            # Without a second opinion we can't claim low risk honestly
            level, reason, confidence = Risk.MED, "second opinion unavailable; routing to clinic", "low"
    elif llm_opinion:
        source = "rules+llm"
        llm_level = {"low": Risk.LOW, "med": Risk.MED, "high": Risk.HIGH}.get(
            str(llm_opinion.get("risk_level", "")).lower())
        if llm_level is None:
            level, reason, confidence = max(level, Risk.MED), reason + "; unreadable second opinion", "low"
        elif llm_level > level:
            level, reason = llm_level, str(llm_opinion.get("reason", "flagged by model"))[:200]
            confidence = llm_opinion.get("confidence", "med")
        elif llm_level == level == Risk.LOW:
            confidence = llm_opinion.get("confidence", "med")

    return RiskAssessment(
        risk_level=level,
        risk_reason=reason,
        confidence=confidence,
        risk_provenance=datetime.now(timezone.utc).isoformat(),
        source=source,
        rule_codes=codes,
        intents=intents,
    )
