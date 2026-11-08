"""Value events: give strangers something worth having before asking for anything.

1. Live statistic: "14 people asked Sunrise Fertility Clinic a question this week."
   It is a live count of real conversations. Below STAT_MIN it isn't shown: a tiny
   number is a privacy risk and a gimmick. A truthful line about reply times
   appears instead. Every display is audited with the value and the query behind
   it, so it can be re-checked. It is *not* counted as a value event: a number
   isn't help.

2. Shareable note (value_kind articulation_draft): at most 240 characters that the
   person can send to a friend or post: an opener, one clinic-sourced fact, a
   question worth asking, and kudos. It is built only from the topic and the
   clinic's own documents, never from what they typed, so it cannot leak anything
   personal. It is unbranded on purpose.

3. Earned email (value_kind conversation_summary): "email me a summary plus questions
   people forget to ask". It is transactional: one email because they asked. More
   needs the separate marketing checkbox, recorded with a timestamp. It covers
   topics and clinic information only; symptoms, medicines and anything personal
   stay in the secure chat.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import yaml

from app.channels import topic_for
from app.config import ClinicConfig
from app.conversion import POLICY_VERSION
from app.knowledge import Chunk, KnowledgeBase, default_kb
from app.logs import log_event
from app.redaction import redact
from app.repository import Consent, LeadContact, LeadSession, Repository

CONTENT_FILE = Path(__file__).parent.parent / "value_content.yaml"
STAT_MIN = 5
STAT_WINDOW_DAYS = 7
NOTE_MAX = 240
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class NotEnoughContext(Exception):
    """We don't know their topic yet; a note or summary would be generic filler."""


class ValueError_(Exception):
    pass


@lru_cache(maxsize=2)
def content(path: Path = CONTENT_FILE) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ live statistic

@dataclass
class LiveStat:
    text: str
    kind: str            # count | alternative
    value: int | None    # the number shown, None for the alternative
    query: str           # what was counted, for re-checking
    window_days: int


def people_this_week(repo: Repository, clinic_id: str, now: datetime) -> int:
    """Distinct people, not conversations: one person with three chats counts once.
    (Found when the first real count said "10 people" for two testers.)"""
    return repo.people_who_asked(clinic_id, (now - timedelta(days=STAT_WINDOW_DAYS)).isoformat())


def live_stat(repo: Repository, clinic: ClinicConfig, now: datetime) -> LiveStat:
    n = people_this_week(repo, clinic.clinic_id, now)
    query = f"distinct people who started a conversation in the last {STAT_WINDOW_DAYS} days"
    if n < STAT_MIN:
        stat = LiveStat(f"Nurses at {clinic.name} usually reply within {clinic.response_window}.",
                        "alternative", None, query, STAT_WINDOW_DAYS)
    else:
        people = "person" if n == 1 else "people"
        stat = LiveStat(f"{n} {people} asked {clinic.name} a question this week.", "count", n, query,
                        STAT_WINDOW_DAYS)
    repo.add_audit("system", "live_stat_shown", kind=stat.kind, value=stat.value, window_days=STAT_WINDOW_DAYS)
    return stat


# ------------------------------------------------------------------ topic

def topic_key(repo: Repository, session: LeadSession) -> str | None:
    """Which content topic fits: what they're looking into or worried about, else the topic they arrived with."""
    topics = content()["topics"]
    current = repo.memory_store(session.id).current()
    candidates = [i.value for k in ("goal", "chief_complaint", "symptom") for i in current
                  if i.kind == k and i.status == "active"]
    candidates.append(topic_for(session) or "")
    for candidate in candidates:
        low = candidate.lower()
        for key in topics:
            if key.lower() in low:
                return key
    return None


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _fact_chunk(key: str, kb: KnowledgeBase) -> Chunk:
    chunks = kb.search(content()["topics"][key]["kb_query"])
    if not chunks:
        raise NotEnoughContext(f"no clinic information for {key}")
    return chunks[0]


def _facts(key: str, kb: KnowledgeBase) -> list[tuple[str, Chunk]]:
    """Candidate facts in the clinic's own order: best-matching section first, and within it the
    first sentence first (usually the plain explanation, not a caveat)."""
    chunks = kb.search(content()["topics"][key]["kb_query"])[:3]
    if not chunks:
        raise NotEnoughContext(f"no clinic information for {key}")
    return [(s, c) for c in chunks for s in _sentences(c.text) if _shareable(s)]


def _shareable(sentence: str) -> bool:
    """A note travels without its context: skip sentences that lean on the one before
    ("It does not…") and urgent-care warnings, which belong in the chat, not a post."""
    return not re.match(r"(it|this|that|they|these|those)\b", sentence, re.I) \
        and not re.search(r"\b(urgent|emergency|999|995)\b", sentence, re.I)


# ------------------------------------------------------------------ shareable note

@dataclass
class ShareNote:
    text: str
    topic: str
    source: Chunk


def compose_note(key: str, kb: KnowledgeBase) -> ShareNote:
    """Fit opener + one cited fact + one question + kudos into 240 characters, preferring the full form."""
    kudos = content()["kudos"]
    openers = [f"It's not always easy to talk about {key}.", f"Talking about {key} isn't easy."]
    facts = _facts(key, kb)
    questions = content()["topics"][key]["questions"]   # most relevant first, as the clinic wrote them
    for fact, chunk in facts:
        for opener in openers:
            for question in questions:
                text = f"{opener} {fact} A question worth asking: {question} {kudos}"
                if len(text) <= NOTE_MAX:
                    return ShareNote(text, key, chunk)
    for fact, chunk in facts:                     # last resort: keep the fact and kudos, drop the question
        for opener in openers:
            text = f"{opener} {fact} {kudos}"
            if len(text) <= NOTE_MAX:
                return ShareNote(text, key, chunk)
    raise NotEnoughContext("could not fit a note")


def share_note(repo: Repository, session: LeadSession, kb: KnowledgeBase | None = None) -> ShareNote:
    key = topic_key(repo, session)
    if key is None:
        raise NotEnoughContext("tell me a little about what you're looking into first")
    note = compose_note(key, kb or default_kb())
    if redact(note.text).entities:            # cannot happen by construction; fail closed if it ever does
        raise NotEnoughContext("note failed the privacy check")
    repo.add_funnel_event(session.id, "value_event", value_kind="articulation_draft")
    repo.advance_stage(session.id, "value_event")
    repo.add_audit("system", "share_note_created", target_table="lead_session", target_id=session.id,
                   chars=len(note.text), topic=key)
    return note


# ------------------------------------------------------------------ earned email

class EmailSender(Protocol):
    def send(self, to: str, subject: str, body: str) -> bool: ...


class OutboxEmailSender:
    """Records emails instead of sending them. Plug in a transactional provider (Resend, SES, Postmark)
    with the same method to send for real."""

    def __init__(self):
        self.outbox: list[dict] = []

    def send(self, to: str, subject: str, body: str) -> bool:
        self.outbox.append({"to": to, "subject": subject, "body": body})
        log_event("email_queued", subject_chars=len(subject), body_chars=len(body))
        return True


def summary_email(key: str | None, clinic: ClinicConfig, kb: KnowledgeBase, marketing: bool) -> tuple[str, str]:
    c = content()
    topic = key or "fertility care"
    questions = list(dict.fromkeys((c["topics"][key]["questions"] if key else []) + c["general"]))[:6]
    lines = [
        "Hi,",
        "",
        f"Here's the summary you asked for from your chat with Nightingale, {clinic.name}'s assistant.",
        "",
        f"What you asked about: {topic}",
    ]
    if key:
        chunk = _fact_chunk(key, kb)
        lines += ["", f"Good to know (from {clinic.name}'s information, \"{chunk.heading}\"):"]
        lines += [f"  {s}" for s in _sentences(chunk.text)[:2]]
    lines += ["", "6 questions people often forget to ask:"]
    lines += [f"  {n}. {q}" for n, q in enumerate(questions, 1)]
    lines += [
        "",
        "For your privacy, this email leaves out the personal details you shared. They stay in your secure "
        "conversation.",
        f"If this is an emergency, call {clinic.emergency_number}.",
        "",
        "You're getting this one email because you asked for it. "
        + ("You also agreed to occasional updates, and you can stop them any time."
           if marketing else "We won't email you again unless you choose to."),
    ]
    return f"Your questions to ask about {topic}", "\n".join(lines)


def email_summary(repo: Repository, session: LeadSession, email: str, marketing_consent: bool,
                  clinic: ClinicConfig, sender: EmailSender, kb: KnowledgeBase | None = None) -> str:
    if not EMAIL.match(email or ""):
        raise ValueError_("a valid email is required")
    key = topic_key(repo, session)
    subject, body = summary_email(key, clinic, kb or default_kb(), marketing_consent)
    if redact(body).entities or redact(subject).entities:   # no identifiers, ever
        raise ValueError_("summary failed the privacy check")
    sent = sender.send(email.strip(), subject, body)

    existing = repo.get_lead_contact(session.id)
    repo.set_lead_contact(LeadContact(session.id, social_platform=existing.social_platform if existing else None,
                                      social_handle=existing.social_handle if existing else None,
                                      volunteered_email=email.strip()))
    if marketing_consent:  # separate checkbox, separate timestamped record; never implied by the email itself
        repo.add_consent(Consent(clinic_id=clinic.clinic_id, kind="marketing", granted=True,
                                 policy_version=POLICY_VERSION, lead_session_id=session.id,
                                 patient_id=session.patient_id))
    repo.add_funnel_event(session.id, "email_captured")
    repo.add_funnel_event(session.id, "value_event", value_kind="conversation_summary")
    repo.advance_stage(session.id, "value_event")
    repo.add_audit("system", "summary_email_sent", target_table="lead_session", target_id=session.id,
                   topic=key, marketing=marketing_consent, sent=sent)
    return subject
