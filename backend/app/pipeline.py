"""One patient message, end to end, with everything persisted.

    1. load the lead session (refuse purged/unknown sessions)
    2. redact once; encrypt the original
    3. risk gate + reply (intake.handle_turn: no advice when escalation is needed)
    4. store the patient message with its risk fields, then the assistant reply
    5. extract memory and apply it, provenance -> the stored patient message
    6. funnel events + stage, audit log (IDs and codes only)

Value events are defined here, in one place. A value_event is a turn where the
patient got substantive help, not just any reply:
    service_answer        a low-risk question answered from the clinic's knowledge base,
                          with at least one citation that checked out
    articulation_draft    a shareable 240-char message drafted for them        (later)
    question_prep         questions to ask the doctor prepared for them         (later)
    conversation_summary  a summary of their conversation sent or shown         (later)
    live_stat             a truthful statistic from a live query                (later)
Safety replies and "please send this to the clinic" are not value events: they
are the right thing to do, but they don't count as us having helped yet.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from app.config import DEFAULT_CLINIC, ClinicConfig
from app.crypto import Cipher
from app.extraction import extract
from app.grounding import MARKER
from app.intake import handle_turn
from app.knowledge import KnowledgeBase, default_kb, tokens
from app.llm import SafeLLM
from app.logs import log_event
from app.memory import apply_operations, profile
from app.redaction import redact
from app.repository import Repository, StoredMessage, now
from app.risk import RiskAssessment


HISTORY_TURNS = 6


class SessionNotFound(Exception):
    pass


class SessionExpired(Exception):
    pass


@dataclass
class PipelineResult:
    patient_message_id: str
    assistant_message_id: str
    reply: str
    risk: RiskAssessment
    show_send_to_clinic: bool
    advice_generated: bool
    clarify: str | None
    profile: dict
    memory_rejections: list[str]
    citations: list[dict]
    reply_confidence: str


def handle_patient_message(repo: Repository, llm: SafeLLM, cipher: Cipher, lead_session_id: str, text: str,
                           clinic: ClinicConfig = DEFAULT_CLINIC, extractor_llm: SafeLLM | None = None,
                           known_identifiers: list[str] | None = None,
                           kb: KnowledgeBase | None = None) -> PipelineResult:
    started = time.perf_counter()
    session = repo.get_lead_session(lead_session_id)
    if session is None:
        raise SessionNotFound(lead_session_id)
    if session.purged_at:
        raise SessionExpired(lead_session_id)

    sent_as = "patient" if session.patient_id else "guest"
    earlier = repo.list_messages(session.id)
    first_message = not any(m.sender == "patient" for m in earlier)
    # Last few turns, redacted, citation markers stripped: context for the reply only
    history = [(m.sender, MARKER.sub("", m.content_redacted).strip()) for m in earlier
               if m.sender in ("patient", "assistant") and m.content_redacted][-HISTORY_TURNS:]

    redaction = redact(text, known_identifiers)
    query = redaction.redacted_text if redaction.ok else ""
    previous = next((content for sender, content in reversed(history) if sender == "patient"), None)
    if previous and len(tokens(query)) < 4:
        query = f"{previous} {query}"  # "how much does it cost?" -> search with what "it" was
    # Retrieval runs locally on redacted text: only clinic documents come back
    sources = (kb or default_kb()).search(query)

    # Fact extraction doesn't depend on the risk check or the reply, so its model
    # call runs alongside them. Only the model call is in the thread: the profile
    # snapshot is read here, and writes happen below, on this thread's connection.
    # (Memory is extracted even for high-risk messages: the clinician needs those facts.)
    store = repo.memory_store(session.id)
    current = store.current()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending_extraction = pool.submit(extract, text, current, extractor_llm)
        turn = handle_turn(text, llm, clinic, known_identifiers, sources=sources, history=history)
        extraction = pending_extraction.result()
    risk = turn.risk

    patient_msg = repo.add_message(StoredMessage(
        lead_session_id=session.id,
        sender="patient",
        sent_as=sent_as,
        content_encrypted=cipher.encrypt(text),
        content_redacted=redaction.redacted_text if redaction.ok else None,
        redaction_ok=redaction.ok,
        redaction_entities=redaction.entities,
        risk_level=risk.risk_level.label,
        risk_reason=risk.risk_reason,
        risk_confidence=risk.confidence,
        risk_source=risk.source,
        risk_assessed_at=risk.risk_provenance,
        risk_rule_codes=risk.rule_codes,
        intents=risk.intents,
        escalation_required=risk.escalation_required,
    ))

    results = apply_operations(store, extraction.operations, patient_msg.id,
                               extracted_by="llm" if extraction.source == "llm" else "rule")

    reply = turn.reply
    if extraction.clarify and not risk.escalation_required:
        reply = f"{reply}\n\n{extraction.clarify}"

    reply_redaction = redact(reply)
    assistant_msg = repo.add_message(StoredMessage(
        lead_session_id=session.id,
        sender="assistant",
        sent_as=sent_as,
        content_encrypted=cipher.encrypt(reply),
        content_redacted=reply_redaction.redacted_text if reply_redaction.ok else None,
        redaction_ok=reply_redaction.ok,
        reply_confidence=turn.reply_confidence,
        advice_generated=turn.advice_generated,
        in_reply_to=patient_msg.id,
    ))
    repo.add_citations(assistant_msg.id, turn.citations)

    if first_message:
        repo.add_funnel_event(session.id, "conversation_started")
        repo.advance_stage(session.id, "conversation_started")
    if turn.advice_generated:
        repo.add_funnel_event(session.id, "value_event", value_kind="service_answer")
        repo.advance_stage(session.id, "value_event")

    repo.add_audit("patient" if session.patient_id else "guest", "message_processed",
                   actor_id=session.patient_id or session.guest_auth_user_id,
                   target_table="message", target_id=patient_msg.id,
                   risk_level=risk.risk_level.label, rule_codes=risk.rule_codes,
                   memory_applied=sum(r.applied for r in results))
    log_event("message_processed", lead_session_id=session.id, message_id=patient_msg.id,
              risk_level=risk.risk_level.label, sent_as=sent_as, at=now(),
              total_ms=int((time.perf_counter() - started) * 1000))

    return PipelineResult(
        patient_message_id=patient_msg.id,
        assistant_message_id=assistant_msg.id,
        reply=reply,
        risk=risk,
        show_send_to_clinic=turn.show_send_to_clinic,
        advice_generated=turn.advice_generated,
        clarify=extraction.clarify,
        profile=profile(store),
        memory_rejections=[r.reason for r in results if not r.applied],
        citations=[{"marker": m, "title": c.title, "heading": c.heading, "source_doc": c.doc,
                    "start_char": c.start, "end_char": c.end, "excerpt": c.text}
                   for m, c in turn.citations],
        reply_confidence=turn.reply_confidence,
    )
