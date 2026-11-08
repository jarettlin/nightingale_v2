"""Send to Clinic, and the clinician review stage after it.

The payload is built so a clinician can start a structured review without
the patient repeating their story:
  * trigger_message_id   the exact message that prompted the send
  * triage_summary       1–5 bullets, each pointing at the messages it came from
  * profile_snapshot     the live profile frozen at send time, with provenance
  * attribution          how this person reached the clinic (Section 1)

Everything in the payload is built deterministically from stored data, not by
an LLM, so every bullet is traceable and nothing is invented.

Promises to the patient are honest and depend on risk. High risk is not a
12–18 hour problem: the patient is told to call emergency services now, and
the escalation sits at the top of the queue.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import DEFAULT_CLINIC, ClinicConfig
from app.crypto import Cipher
from app.memory import profile
from app.redaction import redact
from app.repository import ClinicianResponse, Escalation, Repository, StoredMessage, now

RESPONSE_HOURS = {"high": 1, "med": 18, "low": 18, None: 18}

# Clinician review stage: who can move an escalation where
TRANSITIONS = {
    "sent": {"acknowledged", "in_review", "closed"},
    "acknowledged": {"in_review", "closed"},
    "in_review": {"responded", "closed"},
    "responded": {"in_review", "closed"},
    "closed": set(),
}
CLINICAL_ROLES = {"nurse", "clinician", "admin"}


class EscalationError(Exception):
    pass


class NeedsAccount(EscalationError):
    """Guests must continue securely first: we can't send health info without consent."""


class NeedsConsent(EscalationError):
    """The patient declined data sharing; ask again rather than send."""


@dataclass
class Staff:
    id: str
    clinic_id: str
    role: str  # staff | nurse | clinician | admin


@dataclass
class SendResult:
    escalation: Escalation
    already_open: bool
    confirmation: str


def _reason(message: StoredMessage) -> str:
    if message.risk_level == "high":
        return "high_risk"
    if message.risk_level == "med":
        return "med_risk"
    for intent in ("wants_diagnosis", "medication_change"):
        if intent in (message.intents or []):
            return intent
    return "patient_request"


def _fact_bullet(label: str, items: list[dict]) -> dict | None:
    if not items:
        return None
    parts = []
    for i in items:
        detail = i["detail"].get("onset") or i["detail"].get("since") or i["detail"].get("effective")
        text = f"{i['value']} ({i['status']}{', ' + detail if detail else ''})"
        parts.append(text + (" ⚠ conflicting entry" if i["conflict_with_id"] else ""))
    return {"text": f"{label}: " + "; ".join(parts),
            "provenance_message_ids": [i["provenance_message_id"] for i in items]}


def build_triage_summary(trigger: StoredMessage, prof: dict, attribution: dict) -> list[dict]:
    reason = trigger.risk_reason or "patient asked for a clinician"
    bullets = [{"text": f"Why now: {reason} (risk {trigger.risk_level}, confidence {trigger.risk_confidence})",
                "provenance_message_ids": [trigger.id]}]
    for label, kinds in (("Main concern", ("chief_complaint", "goal")),
                         ("Symptoms", ("symptom",)),
                         ("Medications and allergies", ("medication", "allergy"))):
        items = [i for k in kinds for i in prof.get(k, [])]
        if bullet := _fact_bullet(label, items):
            bullets.append(bullet)
    asks = [i.replace("_", " ") for i in (trigger.intents or []) if i in ("wants_diagnosis", "medication_change")]
    context = f"Came via {attribution['source_channel']}"
    if attribution.get("referral_topic"):
        context += f", staff referral: {attribution['referral_topic']}"
    elif attribution.get("campaign_id"):
        context += f", campaign {attribution['campaign_id']}"
    if asks:
        context = f"Patient asked about: {', '.join(asks)}. {context}"
    bullets.append({"text": context, "provenance_message_ids": [trigger.id] if asks else []})
    return bullets[:5]


def confirmation_message(escalation: Escalation, clinic: ClinicConfig, already_open: bool = False) -> str:
    if escalation.risk_level == "high":
        return (f"I've sent this to {clinic.name} and marked it urgent. Please don't wait for their reply: "
                f"call {clinic.emergency_number} now or go to the nearest emergency department.")
    lead = ("Your earlier request is still with the nurse team, and they can see everything you've added since."
            if already_open else f"Sent to the nurse team at {clinic.name}, with everything you've told me.")
    return (f"{lead} They usually reply within {clinic.response_window}. You can keep chatting with me in the "
            f"meantime, and they'll see anything new. If things get worse, call {clinic.emergency_number}.")


def send_to_clinic(repo: Repository, cipher: Cipher, lead_session_id: str, trigger_message_id: str | None = None,
                   clinic: ClinicConfig = DEFAULT_CLINIC) -> SendResult:
    session = repo.get_lead_session(lead_session_id)
    if session is None:
        raise EscalationError("unknown session")
    if session.patient_id is None:
        raise NeedsAccount("continue securely before sending to the clinic")
    consents = [c for c in repo.consents(session.patient_id) if c.kind == "data_sharing"]
    if not consents or not max(consents, key=lambda c: c.recorded_at).granted:
        raise NeedsConsent("patient has not agreed to share health information with the clinic")

    if trigger_message_id is None:
        # Default: the latest message that needed a clinician, else the latest message
        patient_messages = [m for m in repo.list_messages(session.id) if m.sender == "patient"]
        candidates = [m for m in patient_messages if m.escalation_required] or patient_messages
        if not candidates:
            raise EscalationError("nothing to send yet")
        trigger_message_id = candidates[-1].id
    trigger = repo.get_message(trigger_message_id)
    if trigger is None or trigger.sender != "patient":
        raise EscalationError("trigger must be one of the patient's own messages")
    trigger_session = repo.get_lead_session(trigger.lead_session_id)
    if trigger_session is None or trigger_session.patient_id != session.patient_id:
        raise EscalationError("trigger message belongs to someone else")

    if (existing := repo.open_escalation_for_patient(session.patient_id)) is not None:
        return SendResult(existing, True, confirmation_message(existing, clinic, already_open=True))

    prof = profile(repo.memory_store(session.id))
    attribution = session.attribution()
    snapshot = [{"kind": kind, **item} for kind, items in prof.items() for item in items]
    created = datetime.now(timezone.utc)
    escalation = repo.add_escalation(Escalation(
        clinic_id=session.clinic_id,
        lead_session_id=session.id,
        patient_id=session.patient_id,
        trigger_message_id=trigger.id,
        reason=_reason(trigger),
        risk_level=trigger.risk_level,
        triage_summary=build_triage_summary(trigger, prof, attribution),
        profile_snapshot=snapshot,
        attribution=attribution,
        response_due_at=(created + timedelta(hours=RESPONSE_HOURS[trigger.risk_level])).isoformat(),
        created_at=created.isoformat(),
        updated_at=created.isoformat(),
    ))

    message = confirmation_message(escalation, clinic)
    repo.add_message(StoredMessage(
        lead_session_id=session.id, sender="system", sent_as="patient",
        content_encrypted=cipher.encrypt(message), content_redacted=message))
    repo.add_funnel_event(session.id, "escalation_sent")
    repo.advance_stage(session.id, "escalation_sent")
    repo.add_audit("patient", "escalation_sent", actor_id=session.guest_auth_user_id,
                   target_table="escalation", target_id=escalation.id,
                   reason=escalation.reason, risk_level=escalation.risk_level)
    return SendResult(escalation, False, message)


# --------------------------------------------------------- clinician stage

def _authorise(repo: Repository, staff: Staff, escalation_id: str) -> Escalation:
    escalation = repo.get_escalation(escalation_id)
    # Same answer for "doesn't exist" and "not your clinic": don't confirm it exists
    if escalation is None or escalation.clinic_id != staff.clinic_id:
        raise EscalationError("escalation not found")
    return escalation


def move(repo: Repository, staff: Staff, escalation_id: str, status: str) -> Escalation:
    escalation = _authorise(repo, staff, escalation_id)
    if status not in TRANSITIONS[escalation.status]:
        raise EscalationError(f"cannot move from {escalation.status} to {status}")
    if status in ("in_review", "responded") and staff.role not in CLINICAL_ROLES:
        raise EscalationError("only nurses and clinicians can review")
    repo.update_escalation(escalation.id, status, assigned_to=staff.id if status == "in_review" else None)
    repo.add_audit("staff", f"escalation_{status}", actor_id=staff.id, target_table="escalation",
                   target_id=escalation.id)
    return repo.get_escalation(escalation.id)


def respond(repo: Repository, cipher: Cipher, staff: Staff, escalation_id: str, body: str,
            visible_to_patient: bool = True) -> ClinicianResponse:
    escalation = _authorise(repo, staff, escalation_id)
    if staff.role not in CLINICAL_ROLES:
        raise EscalationError("only nurses and clinicians can respond")
    if escalation.status == "closed":
        raise EscalationError("escalation is closed")
    if escalation.status in ("sent", "acknowledged"):
        move(repo, staff, escalation.id, "in_review")

    response = ClinicianResponse(escalation_id=escalation.id, author_id=staff.id,
                                 body_encrypted=cipher.encrypt(body), visible_to_patient=visible_to_patient)
    repo.add_clinician_response(response)
    if visible_to_patient:
        redacted = redact(body)
        repo.add_message(StoredMessage(
            lead_session_id=escalation.lead_session_id, sender="clinician", sent_as="patient",
            content_encrypted=response.body_encrypted,
            content_redacted=redacted.redacted_text if redacted.ok else None, redaction_ok=redacted.ok))
    move(repo, staff, escalation.id, "responded")
    return response


def review_packet(repo: Repository, cipher: Cipher, staff: Staff, escalation_id: str) -> dict:
    """What a clinician opens: payload plus the original words behind every bullet."""
    escalation = _authorise(repo, staff, escalation_id)
    ids = {escalation.trigger_message_id}
    ids |= {mid for b in escalation.triage_summary for mid in b["provenance_message_ids"]}
    ids |= {item["provenance_message_id"] for item in escalation.profile_snapshot}
    sources = {}
    for mid in ids:
        m = repo.get_message(mid)
        if m is not None:
            sources[mid] = {"text": cipher.decrypt(m.content_encrypted), "sent_as": m.sent_as,
                            "created_at": m.created_at, "risk_level": m.risk_level}
    repo.add_audit("staff", "escalation_viewed", actor_id=staff.id, target_table="escalation",
                   target_id=escalation.id, sources=len(sources))
    return {"escalation": escalation, "sources": sources,
            "responses": repo.clinician_responses(escalation.id)}
