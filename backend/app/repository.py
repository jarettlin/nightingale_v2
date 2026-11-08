"""Persistence behind one interface.

`InMemoryRepo` backs the tests and the demo; `SupabaseRepo` (supabase_repo.py)
implements the same methods against the real tables. The pipeline only ever
sees this interface, so its logic is tested without a network.
"""

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from app.memory import MemoryItem

STAGES = ["visitor", "conversation_started", "value_event", "auth_started",
          "consented", "patient_created", "escalation_sent"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LeadSession:
    id: str
    clinic_id: str
    source_channel: str
    identity_level: str
    campaign_id: str | None = None
    creative: str | None = None
    landing_page: str | None = None
    referral_topic: str | None = None
    guest_auth_user_id: str | None = None
    patient_id: str | None = None
    stage: str = "visitor"
    landing_timestamp: str = field(default_factory=now)
    expires_at: str = field(default_factory=lambda: (datetime.now(timezone.utc) + timedelta(days=7)).isoformat())
    purged_at: str | None = None
    converted_at: str | None = None
    referred_by: str | None = None          # staff member who made a referral link
    resume_token_hash: str | None = None    # sha256 of the token in a referral/comment/lead-form link

    def attribution(self) -> dict:
        """What must survive to the patient record and every escalation."""
        return {k: getattr(self, k) for k in (
            "clinic_id", "source_channel", "campaign_id", "creative", "landing_page",
            "identity_level", "landing_timestamp", "referral_topic")} | {"lead_session_id": self.id}


@dataclass
class StoredMessage:
    lead_session_id: str
    sender: str                         # patient | assistant | clinician | system
    sent_as: str                        # guest | patient
    content_encrypted: str
    content_redacted: str | None
    redaction_ok: bool = True
    redaction_entities: list = field(default_factory=list)
    risk_level: str | None = None
    risk_reason: str | None = None
    risk_confidence: str | None = None
    risk_source: str | None = None
    risk_assessed_at: str | None = None
    risk_rule_codes: list = field(default_factory=list)
    intents: list = field(default_factory=list)
    escalation_required: bool | None = None
    reply_confidence: str | None = None
    advice_generated: bool | None = None
    in_reply_to: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=now)


@dataclass
class LeadContact:
    """Identity a guest volunteered or the channel told us (e.g. their Instagram handle)."""
    lead_session_id: str
    social_platform: str | None = None
    social_handle: str | None = None
    volunteered_email: str | None = None


@dataclass
class ContactPoint:
    patient_id: str
    kind: str                           # email | phone | instagram | tiktok | facebook | whatsapp
    value: str
    verified_at: str | None = None
    is_primary: bool = True
    valid_from: str = field(default_factory=now)
    valid_to: str | None = None


@dataclass
class Consent:
    clinic_id: str
    kind: str                           # data_sharing | marketing | transactional_email
    granted: bool
    policy_version: str
    patient_id: str | None = None
    lead_session_id: str | None = None
    recorded_at: str = field(default_factory=now)


OPEN_ESCALATION = ("sent", "acknowledged", "in_review")


@dataclass
class Escalation:
    clinic_id: str
    lead_session_id: str
    patient_id: str
    trigger_message_id: str
    reason: str                         # high_risk | med_risk | wants_diagnosis | medication_change | patient_request
    risk_level: str | None
    triage_summary: list                # [{"text", "provenance_message_ids"}], 1–5 items
    profile_snapshot: list              # current facts at send time, with provenance
    attribution: dict                   # acquisition context, frozen at send time
    response_due_at: str
    status: str = "sent"
    assigned_to: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)


@dataclass
class ClinicianResponse:
    escalation_id: str
    author_id: str
    body_encrypted: str
    visible_to_patient: bool = True
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=now)


def citation_rows(message_id: str, citations: list) -> list[dict]:
    """(marker, Chunk) pairs -> rows for the citation table."""
    return [{"message_id": message_id, "marker": marker, "source_doc": c.doc, "source_version": c.version,
             "start_char": c.start, "end_char": c.end} for marker, c in citations]


class Repository(Protocol):
    def add_citations(self, message_id: str, citations: list) -> None: ...
    def citations_for(self, message_id: str) -> list[dict]: ...
    def find_staff_by_auth_user(self, auth_user_id: str): ...
    def get_patient(self, patient_id: str) -> dict | None: ...
    def list_patients(self, clinic_id: str) -> list[str]: ...
    def sessions_for_patient(self, patient_id: str) -> list[LeadSession]: ...
    def sessions_for_auth_user(self, auth_user_id: str) -> list[LeadSession]: ...
    def escalations_for_patient(self, patient_id: str) -> list[Escalation]: ...
    def add_escalation(self, escalation: Escalation) -> Escalation: ...
    def get_escalation(self, escalation_id: str) -> Escalation | None: ...
    def open_escalation_for_patient(self, patient_id: str) -> Escalation | None: ...
    def update_escalation(self, escalation_id: str, status: str, assigned_to: str | None = None) -> None: ...
    def escalation_queue(self, clinic_id: str) -> list[Escalation]: ...
    def add_clinician_response(self, response: ClinicianResponse) -> None: ...
    def clinician_responses(self, escalation_id: str) -> list[ClinicianResponse]: ...
    def create_lead_session(self, log_visit: bool = True, **fields) -> LeadSession: ...
    def find_session_by_token_hash(self, token_hash: str) -> LeadSession | None: ...
    def claim_session(self, lead_session_id: str, guest_auth_user_id: str) -> bool: ...
    def recent_session_for_handle(self, platform: str, handle: str, since: str) -> LeadSession | None: ...
    def lead_sessions(self, clinic_id: str, since: str) -> list[LeadSession]: ...
    def funnel_rows(self, clinic_id: str, since: str) -> list[dict]: ...
    def get_lead_session(self, lead_session_id: str) -> LeadSession | None: ...
    def advance_stage(self, lead_session_id: str, stage: str) -> None: ...
    def set_lead_contact(self, contact: LeadContact) -> None: ...
    def get_lead_contact(self, lead_session_id: str) -> LeadContact | None: ...
    def create_patient(self, clinic_id: str, auth_user_id: str | None) -> str: ...
    def find_patient_by_email(self, clinic_id: str, email: str) -> str | None: ...
    def find_patient_by_auth_user(self, auth_user_id: str) -> str | None: ...
    def upsert_contact_point(self, contact: ContactPoint) -> None: ...
    def contact_points(self, patient_id: str) -> list[ContactPoint]: ...
    def add_consent(self, consent: Consent) -> None: ...
    def consents(self, patient_id: str) -> list[Consent]: ...
    def lead_consents(self, lead_session_id: str) -> list[Consent]: ...
    def link_session_to_patient(self, lead_session_id: str, patient_id: str) -> None: ...
    def add_message(self, message: StoredMessage) -> StoredMessage: ...
    def list_messages(self, lead_session_id: str) -> list[StoredMessage]: ...
    def get_message(self, message_id: str) -> StoredMessage | None: ...
    def memory_store(self, lead_session_id: str): ...
    def add_funnel_event(self, lead_session_id: str, event: str, value_kind: str | None = None) -> None: ...
    def add_audit(self, actor_type: str, action: str, actor_id: str | None = None,
                  target_table: str | None = None, target_id: str | None = None, **metadata) -> None: ...


def later_stage(current: str, proposed: str) -> str:
    """The funnel only moves forward."""
    return proposed if STAGES.index(proposed) > STAGES.index(current) else current


class _SessionMemory:
    """A patient's memory across all their lead sessions; new facts are written
    to the session the message came from, so provenance stays exact."""

    def __init__(self, repo: "InMemoryRepo", lead_session_id: str):
        self._repo, self._session_id = repo, lead_session_id

    def _items(self) -> list[MemoryItem]:
        group = self._repo.session_group(self._session_id)
        return [item for sid, item in self._repo.memory_rows if sid in group]

    def append(self, item: MemoryItem) -> MemoryItem:
        self._repo.memory_rows.append((self._session_id, item))
        return item

    def all(self) -> list[MemoryItem]:
        return self._items()

    def get(self, item_id: str) -> MemoryItem | None:
        return next((i for i in self._items() if i.id == item_id), None)

    def current(self) -> list[MemoryItem]:
        items = self._items()
        replaced = {i.supersedes_id for i in items if i.supersedes_id}
        return [i for i in items if i.id not in replaced]


class InMemoryRepo:
    def __init__(self):
        self.sessions: dict[str, LeadSession] = {}
        self.messages: list[StoredMessage] = []
        self.memory_rows: list[tuple[str, MemoryItem]] = []
        self.funnel_events: list[dict] = []
        self.audit_log: list[dict] = []
        self.lead_contacts: dict[str, LeadContact] = {}
        self.patients: dict[str, dict] = {}
        self.contacts: list[ContactPoint] = []
        self.consent_rows: list[Consent] = []
        self.escalations: dict[str, Escalation] = {}
        self.responses: list[ClinicianResponse] = []
        self.staff: dict[str, object] = {}       # auth_user_id -> escalation.Staff
        self.citations: list[dict] = []

    def add_citations(self, message_id: str, citations: list) -> None:
        self.citations += citation_rows(message_id, citations)

    def citations_for(self, message_id: str) -> list[dict]:
        return [c for c in self.citations if c["message_id"] == message_id]

    def add_staff(self, auth_user_id: str, staff) -> None:
        self.staff[auth_user_id] = staff

    def find_staff_by_auth_user(self, auth_user_id: str):
        return self.staff.get(auth_user_id)

    def get_patient(self, patient_id: str) -> dict | None:
        p = self.patients.get(patient_id)
        return {"id": patient_id, **p} if p else None

    def list_patients(self, clinic_id: str) -> list[str]:
        return [pid for pid, p in self.patients.items() if p["clinic_id"] == clinic_id]

    def sessions_for_patient(self, patient_id: str) -> list[LeadSession]:
        return [s for s in self.sessions.values() if s.patient_id == patient_id]

    def sessions_for_auth_user(self, auth_user_id: str) -> list[LeadSession]:
        """Everything this user owns: guest sessions they started, plus sessions linked to their patient record."""
        patient_id = self.find_patient_by_auth_user(auth_user_id)
        return [s for s in self.sessions.values() if s.purged_at is None and (
            s.guest_auth_user_id == auth_user_id or (patient_id is not None and s.patient_id == patient_id))]

    def escalations_for_patient(self, patient_id: str) -> list[Escalation]:
        return sorted((e for e in self.escalations.values() if e.patient_id == patient_id),
                      key=lambda e: e.created_at)

    def add_escalation(self, escalation: Escalation) -> Escalation:
        self.escalations[escalation.id] = escalation
        return escalation

    def get_escalation(self, escalation_id: str) -> Escalation | None:
        return self.escalations.get(escalation_id)

    def open_escalation_for_patient(self, patient_id: str) -> Escalation | None:
        return next((e for e in self.escalations.values()
                     if e.patient_id == patient_id and e.status in OPEN_ESCALATION), None)

    def update_escalation(self, escalation_id: str, status: str, assigned_to: str | None = None) -> None:
        e = self.escalations[escalation_id]
        e.status, e.updated_at = status, now()
        e.assigned_to = assigned_to or e.assigned_to

    def escalation_queue(self, clinic_id: str) -> list[Escalation]:
        # Highest risk first, then oldest: a compassion queue, not a sales list
        rank = {"high": 0, "med": 1, "low": 2, None: 3}
        return sorted((e for e in self.escalations.values() if e.clinic_id == clinic_id and e.status != "closed"),
                      key=lambda e: (rank[e.risk_level], e.created_at))

    def add_clinician_response(self, response: ClinicianResponse) -> None:
        self.responses.append(response)

    def clinician_responses(self, escalation_id: str) -> list[ClinicianResponse]:
        return [r for r in self.responses if r.escalation_id == escalation_id]

    def set_lead_contact(self, contact: LeadContact) -> None:
        self.lead_contacts[contact.lead_session_id] = contact

    def get_lead_contact(self, lead_session_id: str) -> LeadContact | None:
        return self.lead_contacts.get(lead_session_id)

    def create_patient(self, clinic_id: str, auth_user_id: str | None) -> str:
        patient_id = str(uuid.uuid4())
        self.patients[patient_id] = {"clinic_id": clinic_id, "auth_user_id": auth_user_id}
        return patient_id

    def find_patient_by_email(self, clinic_id: str, email: str) -> str | None:
        return next((c.patient_id for c in self.contacts
                     if c.kind == "email" and c.value.lower() == email.lower() and c.valid_to is None
                     and c.verified_at and self.patients[c.patient_id]["clinic_id"] == clinic_id), None)

    def find_patient_by_auth_user(self, auth_user_id: str) -> str | None:
        return next((pid for pid, p in self.patients.items() if p["auth_user_id"] == auth_user_id), None)

    def upsert_contact_point(self, contact: ContactPoint) -> None:
        for c in self.contacts:
            if c.patient_id == contact.patient_id and c.kind == contact.kind and c.valid_to is None:
                if c.value.lower() == contact.value.lower():
                    c.verified_at = c.verified_at or contact.verified_at
                    return
                c.valid_to = now()  # replaced, not deleted: history survives
        self.contacts.append(contact)

    def contact_points(self, patient_id: str) -> list[ContactPoint]:
        return [c for c in self.contacts if c.patient_id == patient_id]

    def add_consent(self, consent: Consent) -> None:
        self.consent_rows.append(consent)

    def consents(self, patient_id: str) -> list[Consent]:
        return [c for c in self.consent_rows if c.patient_id == patient_id]

    def lead_consents(self, lead_session_id: str) -> list[Consent]:
        return [c for c in self.consent_rows if c.lead_session_id == lead_session_id]

    def link_session_to_patient(self, lead_session_id: str, patient_id: str) -> None:
        session = self.sessions[lead_session_id]
        if session.patient_id not in (None, patient_id):
            raise ValueError("session already belongs to another patient")
        if session.patient_id is None:
            session.patient_id, session.converted_at = patient_id, now()

    def create_lead_session(self, log_visit: bool = True, **fields) -> LeadSession:
        # log_visit=False for sessions prepared ahead (referral, comment, lead-form links):
        # the visit is logged when the person actually opens the link
        session = LeadSession(id=str(uuid.uuid4()), **fields)
        self.sessions[session.id] = session
        if log_visit:
            self.add_funnel_event(session.id, "visitor")
        return session

    def find_session_by_token_hash(self, token_hash: str) -> LeadSession | None:
        return next((s for s in self.sessions.values() if s.resume_token_hash == token_hash), None)

    def claim_session(self, lead_session_id: str, guest_auth_user_id: str) -> bool:
        """Give an unowned, prepared session to the person who opened its link. First opener wins."""
        s = self.sessions[lead_session_id]
        if s.guest_auth_user_id is None and s.patient_id is None:
            s.guest_auth_user_id = guest_auth_user_id
            return True
        return s.guest_auth_user_id == guest_auth_user_id

    def recent_session_for_handle(self, platform: str, handle: str, since: str) -> LeadSession | None:
        for sid, c in self.lead_contacts.items():
            s = self.sessions.get(sid)
            if (c.social_platform == platform and (c.social_handle or "").lower() == handle.lower()
                    and s and s.landing_timestamp >= since):
                return s
        return None

    def lead_sessions(self, clinic_id: str, since: str) -> list[LeadSession]:
        return [s for s in self.sessions.values()
                if s.clinic_id == clinic_id and s.purged_at is None and s.landing_timestamp >= since]

    def funnel_rows(self, clinic_id: str, since: str) -> list[dict]:
        return [e for e in self.funnel_events if e["clinic_id"] == clinic_id and e["created_at"] >= since]

    def get_lead_session(self, lead_session_id: str) -> LeadSession | None:
        return self.sessions.get(lead_session_id)

    def advance_stage(self, lead_session_id: str, stage: str) -> None:
        session = self.sessions[lead_session_id]
        session.stage = later_stage(session.stage, stage)

    def session_group(self, lead_session_id: str) -> set[str]:
        session = self.sessions[lead_session_id]
        if session.patient_id is None:
            return {session.id}
        return {s.id for s in self.sessions.values() if s.patient_id == session.patient_id}

    def add_message(self, message: StoredMessage) -> StoredMessage:
        self.messages.append(message)
        return message

    def list_messages(self, lead_session_id: str) -> list[StoredMessage]:
        return [m for m in self.messages if m.lead_session_id == lead_session_id]

    def get_message(self, message_id: str) -> StoredMessage | None:
        return next((m for m in self.messages if m.id == message_id), None)

    def memory_store(self, lead_session_id: str) -> _SessionMemory:
        return _SessionMemory(self, lead_session_id)

    def add_funnel_event(self, lead_session_id: str, event: str, value_kind: str | None = None) -> None:
        session = self.sessions[lead_session_id]
        self.funnel_events.append({"lead_session_id": lead_session_id, "clinic_id": session.clinic_id,
                                   "source_channel": session.source_channel, "event": event,
                                   "value_kind": value_kind, "created_at": now()})

    def add_audit(self, actor_type: str, action: str, actor_id: str | None = None,
                  target_table: str | None = None, target_id: str | None = None, **metadata) -> None:
        self.audit_log.append({"actor_type": actor_type, "actor_id": actor_id, "action": action,
                               "target_table": target_table, "target_id": target_id,
                               "metadata": metadata, "created_at": now()})


def message_row(message: StoredMessage) -> dict:
    return asdict(message)
