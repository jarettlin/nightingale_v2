"""One patient message, end to end, with everything persisted.

    1. load the lead session (refuse purged/unknown sessions)
    2. redact once; encrypt the original
    3. risk gate + reply (intake.handle_turn: no advice when escalation is needed)
    4. store the patient message with its risk fields, then the assistant reply
    5. extract memory and apply it, provenance -> the stored patient message
    6. funnel events + stage, audit log (IDs and codes only)

Value events are defined here, in one place. A value_event is a turn where the
patient got substantive help, not just any reply:
    service_answer        a low-risk question answered with general information
    articulation_draft    a shareable 240-char message drafted for them        (later)
    question_prep         questions to ask the doctor prepared for them         (later)
    conversation_summary  a summary of their conversation sent or shown         (later)
    live_stat             a truthful statistic from a live query                (later)
Safety replies and "please send this to the clinic" are not value events: they
are the right thing to do, but they don't count as us having helped yet.
"""

from dataclasses import dataclass

from app.config import DEFAULT_CLINIC, ClinicConfig
from app.crypto import Cipher
from app.extraction import extract
from app.intake import handle_turn
from app.llm import SafeLLM
from app.logs import log_event
from app.memory import apply_operations, profile
from app.redaction import redact
from app.repository import Repository, StoredMessage, now
from app.risk import RiskAssessment


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


def handle_patient_message(repo: Repository, llm: SafeLLM, cipher: Cipher, lead_session_id: str, text: str,
                           clinic: ClinicConfig = DEFAULT_CLINIC, extractor_llm: SafeLLM | None = None,
                           known_identifiers: list[str] | None = None) -> PipelineResult:
    session = repo.get_lead_session(lead_session_id)
    if session is None:
        raise SessionNotFound(lead_session_id)
    if session.purged_at:
        raise SessionExpired(lead_session_id)

    sent_as = "patient" if session.patient_id else "guest"
    first_message = not any(m.sender == "patient" for m in repo.list_messages(session.id))

    redaction = redact(text, known_identifiers)
    turn = handle_turn(text, llm, clinic, known_identifiers)
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

    # Memory is extracted even for high-risk messages: the clinician needs those facts
    store = repo.memory_store(session.id)
    extraction = extract(text, store.current(), extractor_llm)
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
        # Fixed safety templates are fully trusted; generated text is "med" until
        # citation checking can vouch for it
        reply_confidence="med" if turn.advice_generated else "high",
        advice_generated=turn.advice_generated,
        in_reply_to=patient_msg.id,
    ))

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
              risk_level=risk.risk_level.label, sent_as=sent_as, at=now())

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
    )
