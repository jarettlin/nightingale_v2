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


class Repository(Protocol):
    def create_lead_session(self, **fields) -> LeadSession: ...
    def get_lead_session(self, lead_session_id: str) -> LeadSession | None: ...
    def advance_stage(self, lead_session_id: str, stage: str) -> None: ...
    def add_message(self, message: StoredMessage) -> StoredMessage: ...
    def list_messages(self, lead_session_id: str) -> list[StoredMessage]: ...
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

    def create_lead_session(self, **fields) -> LeadSession:
        session = LeadSession(id=str(uuid.uuid4()), **fields)
        self.sessions[session.id] = session
        self.add_funnel_event(session.id, "visitor")
        return session

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
