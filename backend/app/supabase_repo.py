"""Repository backed by the Supabase Postgres tables (see supabase/migrations).

The backend connects as the database owner, which bypasses RLS: that's the
"only the backend writes" half of the design. Reads on behalf of a user will
go through `as_user()` so RLS applies to them (used by the API layer).

The repo never commits. The caller owns the transaction: one per request in
the API, and a rolled-back one in the integration test.
"""

import json
import os
import uuid
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.memory import MemoryItem
from app.repository import (ClinicianResponse, Consent, ContactPoint, Escalation, LeadContact, LeadSession,
                            StoredMessage)

LEAD_FIELDS = ["id", "clinic_id", "source_channel", "identity_level", "campaign_id", "creative",
               "landing_page", "referral_topic", "guest_auth_user_id", "patient_id", "stage",
               "landing_timestamp", "expires_at", "purged_at", "converted_at"]
MESSAGE_FIELDS = ["id", "lead_session_id", "sender", "sent_as", "content_encrypted", "content_redacted",
                  "redaction_ok", "redaction_entities", "risk_level", "risk_reason", "risk_confidence",
                  "risk_source", "risk_assessed_at", "risk_rule_codes", "intents", "escalation_required",
                  "reply_confidence", "advice_generated", "in_reply_to", "created_at"]
ESCALATION_FIELDS = ["id", "clinic_id", "lead_session_id", "patient_id", "trigger_message_id", "reason",
                     "risk_level", "triage_summary", "profile_snapshot", "attribution", "response_due_at",
                     "status", "assigned_to", "created_at", "updated_at"]
MEMORY_FIELDS = ["id", "kind", "value", "status", "provenance_message_id", "updated_at", "detail",
                 "supersedes_id", "conflict_with_id", "extracted_by"]


def connect(url: str | None = None) -> psycopg.Connection:
    url = url or os.environ["DATABASE_URL"]
    if not url.startswith(("postgresql://", "postgres://")):
        raise ValueError("DATABASE_URL must be the Postgres URI (postgresql://...), from Supabase → Connect → "
                         "Session pooler, not the https:// project URL")
    return psycopg.connect(url, row_factory=dict_row, autocommit=False)


def _str(row: dict) -> dict:
    """uuid/datetime -> str, so rows match the in-memory dataclasses exactly."""
    return {k: (str(v) if v is not None and not isinstance(v, (str, bool, int, float, list, dict)) else v)
            for k, v in row.items()}


class _DbMemory:
    def __init__(self, conn: psycopg.Connection, lead_session_id: str):
        self._conn, self._session_id = conn, lead_session_id

    def _rows(self, where: str = "", params: tuple = ()) -> list[MemoryItem]:
        rows = self._conn.execute(
            f"""select {", ".join(MEMORY_FIELDS)} from public.memory_item
                where lead_session_id in (
                    select id from public.lead_session
                    where id = %s
                       or (patient_id is not null
                           and patient_id = (select patient_id from public.lead_session where id = %s)))
                {where}
                order by updated_at""",
            (self._session_id, self._session_id, *params)).fetchall()
        return [MemoryItem(**_str(r)) for r in rows]

    def append(self, item: MemoryItem) -> MemoryItem:
        self._conn.execute(
            f"""insert into public.memory_item (lead_session_id, {", ".join(MEMORY_FIELDS)})
                values (%s, {", ".join(["%s"] * len(MEMORY_FIELDS))})""",
            (self._session_id, item.id, item.kind, item.value, item.status, item.provenance_message_id,
             item.updated_at, Jsonb(item.detail), item.supersedes_id, item.conflict_with_id,
             item.extracted_by))
        return item

    def all(self) -> list[MemoryItem]:
        return self._rows()

    def get(self, item_id: str) -> MemoryItem | None:
        rows = self._rows("and memory_item.id = %s", (item_id,))
        return rows[0] if rows else None

    def current(self) -> list[MemoryItem]:
        return self._rows("and not exists (select 1 from public.memory_item n where n.supersedes_id = memory_item.id)")


class SupabaseRepo:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def create_lead_session(self, **fields) -> LeadSession:
        cols = list(fields)
        row = self.conn.execute(
            f"""insert into public.lead_session ({", ".join(cols)})
                values ({", ".join(["%s"] * len(cols))})
                returning {", ".join(LEAD_FIELDS)}""",
            tuple(fields.values())).fetchone()
        session = LeadSession(**_str(row))
        self.add_funnel_event(session.id, "visitor")
        return session

    def get_lead_session(self, lead_session_id: str) -> LeadSession | None:
        # Check the shape in Python: a SQL cast error would abort the request's transaction
        try:
            uuid.UUID(lead_session_id)
        except ValueError:
            return None
        row = self.conn.execute(
            f"select {', '.join(LEAD_FIELDS)} from public.lead_session where id = %s",
            (lead_session_id,)).fetchone()
        return LeadSession(**_str(row)) if row else None

    def advance_stage(self, lead_session_id: str, stage: str) -> None:
        # Forward-only, decided in SQL so concurrent requests can't move it back
        self.conn.execute(
            """update public.lead_session set stage = %(stage)s, last_active_at = now()
               where id = %(id)s
                 and array_position(%(order)s::text[], stage) < array_position(%(order)s::text[], %(stage)s)""",
            {"id": lead_session_id, "stage": stage,
             "order": ["visitor", "conversation_started", "value_event", "auth_started",
                       "consented", "patient_created", "escalation_sent"]})

    def add_message(self, message: StoredMessage) -> StoredMessage:
        values = {f: getattr(message, f) for f in MESSAGE_FIELDS}
        values["redaction_entities"] = Jsonb(values["redaction_entities"])
        self.conn.execute(
            f"""insert into public.message ({", ".join(MESSAGE_FIELDS)})
                values ({", ".join(f"%({f})s" for f in MESSAGE_FIELDS)})""", values)
        return message

    def list_messages(self, lead_session_id: str) -> list[StoredMessage]:
        rows = self.conn.execute(
            f"select {', '.join(MESSAGE_FIELDS)} from public.message where lead_session_id = %s order by created_at",
            (lead_session_id,)).fetchall()
        return [StoredMessage(**_str(r)) for r in rows]

    def get_message(self, message_id: str) -> StoredMessage | None:
        row = self.conn.execute(f"select {', '.join(MESSAGE_FIELDS)} from public.message where id = %s",
                                (message_id,)).fetchone()
        return StoredMessage(**_str(row)) if row else None

    def memory_store(self, lead_session_id: str) -> _DbMemory:
        return _DbMemory(self.conn, lead_session_id)

    def add_funnel_event(self, lead_session_id: str, event: str, value_kind: str | None = None) -> None:
        self.conn.execute(
            """insert into public.funnel_event (clinic_id, lead_session_id, source_channel, event, value_kind)
               select clinic_id, id, source_channel, %s, %s from public.lead_session where id = %s""",
            (event, value_kind, lead_session_id))

    def add_audit(self, actor_type: str, action: str, actor_id: str | None = None,
                  target_table: str | None = None, target_id: str | None = None, **metadata) -> None:
        # actor_id must be a uuid in the table; anonymous-user ids are, test strings aren't
        self.conn.execute(
            """insert into public.audit_log (actor_type, actor_id, action, target_table, target_id, metadata)
               values (%s, %s, %s, %s, %s, %s)""",
            (actor_type, actor_id, action, target_table, target_id, Jsonb(json.loads(json.dumps(metadata)))))

    def set_lead_contact(self, contact: LeadContact) -> None:
        self.conn.execute(
            """insert into public.lead_contact (lead_session_id, social_platform, social_handle, volunteered_email)
               values (%s, %s, %s, %s)
               on conflict (lead_session_id) do update
                 set social_platform = excluded.social_platform, social_handle = excluded.social_handle,
                     volunteered_email = excluded.volunteered_email""",
            (contact.lead_session_id, contact.social_platform, contact.social_handle, contact.volunteered_email))

    def get_lead_contact(self, lead_session_id: str) -> LeadContact | None:
        row = self.conn.execute(
            """select lead_session_id, social_platform, social_handle, volunteered_email
               from public.lead_contact where lead_session_id = %s""", (lead_session_id,)).fetchone()
        return LeadContact(**_str(row)) if row else None

    def create_patient(self, clinic_id: str, auth_user_id: str | None) -> str:
        row = self.conn.execute("insert into public.patient (clinic_id, auth_user_id) values (%s, %s) returning id",
                                (clinic_id, auth_user_id)).fetchone()
        return str(row["id"])

    def find_patient_by_email(self, clinic_id: str, email: str) -> str | None:
        row = self.conn.execute(
            """select p.id from public.patient p
               join public.contact_point c on c.patient_id = p.id
               where p.clinic_id = %s and c.kind = 'email' and lower(c.value) = lower(%s)
                 and c.valid_to is null and c.verified_at is not null
               limit 1""", (clinic_id, email)).fetchone()
        return str(row["id"]) if row else None

    def find_patient_by_auth_user(self, auth_user_id: str) -> str | None:
        row = self.conn.execute("select id from public.patient where auth_user_id = %s",
                                (auth_user_id,)).fetchone()
        return str(row["id"]) if row else None

    def upsert_contact_point(self, contact: ContactPoint) -> None:
        current = self.conn.execute(
            """select id, value from public.contact_point
               where patient_id = %s and kind = %s and valid_to is null and is_primary""",
            (contact.patient_id, contact.kind)).fetchone()
        if current and current["value"].lower() == contact.value.lower():
            self.conn.execute(
                "update public.contact_point set verified_at = coalesce(verified_at, %s) where id = %s",
                (contact.verified_at, current["id"]))
            return
        if current:  # replaced, not deleted: history survives
            self.conn.execute("update public.contact_point set valid_to = now() where id = %s", (current["id"],))
        self.conn.execute(
            """insert into public.contact_point (patient_id, kind, value, verified_at, is_primary, valid_from)
               values (%s, %s, %s, %s, %s, %s)""",
            (contact.patient_id, contact.kind, contact.value, contact.verified_at, contact.is_primary,
             contact.valid_from))

    def contact_points(self, patient_id: str) -> list[ContactPoint]:
        rows = self.conn.execute(
            """select patient_id, kind, value, verified_at, is_primary, valid_from, valid_to
               from public.contact_point where patient_id = %s order by valid_from""", (patient_id,)).fetchall()
        return [ContactPoint(**_str(r)) for r in rows]

    def add_consent(self, consent: Consent) -> None:
        self.conn.execute(
            """insert into public.consent (clinic_id, lead_session_id, patient_id, kind, granted, policy_version, recorded_at)
               values (%s, %s, %s, %s, %s, %s, %s)""",
            (consent.clinic_id, consent.lead_session_id, consent.patient_id, consent.kind, consent.granted,
             consent.policy_version, consent.recorded_at))

    def consents(self, patient_id: str) -> list[Consent]:
        rows = self.conn.execute(
            """select clinic_id, kind, granted, policy_version, patient_id, lead_session_id, recorded_at
               from public.consent where patient_id = %s order by recorded_at""", (patient_id,)).fetchall()
        return [Consent(**_str(r)) for r in rows]

    def link_session_to_patient(self, lead_session_id: str, patient_id: str) -> None:
        row = self.conn.execute(
            """update public.lead_session set patient_id = %s, converted_at = now()
               where id = %s and patient_id is null returning id""", (patient_id, lead_session_id)).fetchone()
        if row is None:
            existing = self.conn.execute("select patient_id from public.lead_session where id = %s",
                                         (lead_session_id,)).fetchone()
            if existing is None or str(existing["patient_id"]) != patient_id:
                raise ValueError("session already belongs to another patient")

    def find_staff_by_auth_user(self, auth_user_id: str):
        from app.escalation import Staff
        row = self.conn.execute("select id, clinic_id, role from public.staff_member where auth_user_id = %s",
                                (auth_user_id,)).fetchone()
        return Staff(**_str(row)) if row else None

    def get_patient(self, patient_id: str) -> dict | None:
        row = self.conn.execute("select id, clinic_id, auth_user_id from public.patient where id = %s",
                                (patient_id,)).fetchone()
        return _str(row) if row else None

    def list_patients(self, clinic_id: str) -> list[str]:
        rows = self.conn.execute("select id from public.patient where clinic_id = %s order by created_at",
                                 (clinic_id,)).fetchall()
        return [str(r["id"]) for r in rows]

    def sessions_for_patient(self, patient_id: str) -> list[LeadSession]:
        rows = self.conn.execute(
            f"select {', '.join(LEAD_FIELDS)} from public.lead_session where patient_id = %s order by created_at",
            (patient_id,)).fetchall()
        return [LeadSession(**_str(r)) for r in rows]

    def escalations_for_patient(self, patient_id: str) -> list[Escalation]:
        return self._escalations("patient_id = %s order by created_at", (patient_id,))

    def add_escalation(self, escalation: Escalation) -> Escalation:
        values = {f: getattr(escalation, f) for f in ESCALATION_FIELDS}
        for f in ("triage_summary", "profile_snapshot", "attribution"):
            values[f] = Jsonb(values[f])
        self.conn.execute(
            f"""insert into public.escalation ({", ".join(ESCALATION_FIELDS)})
                values ({", ".join(f"%({f})s" for f in ESCALATION_FIELDS)})""", values)
        return escalation

    def _escalations(self, where: str, params: tuple) -> list[Escalation]:
        rows = self.conn.execute(
            f"select {', '.join(ESCALATION_FIELDS)} from public.escalation where {where}", params).fetchall()
        return [Escalation(**_str(r)) for r in rows]

    def get_escalation(self, escalation_id: str) -> Escalation | None:
        rows = self._escalations("id = %s", (escalation_id,))
        return rows[0] if rows else None

    def open_escalation_for_patient(self, patient_id: str) -> Escalation | None:
        rows = self._escalations("patient_id = %s and status in ('sent', 'acknowledged', 'in_review') "
                                 "order by created_at desc limit 1", (patient_id,))
        return rows[0] if rows else None

    def update_escalation(self, escalation_id: str, status: str, assigned_to: str | None = None) -> None:
        self.conn.execute(
            """update public.escalation set status = %s, assigned_to = coalesce(%s, assigned_to), updated_at = now()
               where id = %s""", (status, assigned_to, escalation_id))

    def escalation_queue(self, clinic_id: str) -> list[Escalation]:
        return self._escalations(
            """clinic_id = %s and status <> 'closed'
               order by case risk_level when 'high' then 0 when 'med' then 1 when 'low' then 2 else 3 end,
                        created_at""", (clinic_id,))

    def add_clinician_response(self, response: ClinicianResponse) -> None:
        self.conn.execute(
            """insert into public.clinician_response (id, escalation_id, author_id, body_encrypted, visible_to_patient, created_at)
               values (%s, %s, %s, %s, %s, %s)""",
            (response.id, response.escalation_id, response.author_id, response.body_encrypted,
             response.visible_to_patient, response.created_at))

    def clinician_responses(self, escalation_id: str) -> list[ClinicianResponse]:
        rows = self.conn.execute(
            """select id, escalation_id, author_id, body_encrypted, visible_to_patient, created_at
               from public.clinician_response where escalation_id = %s order by created_at""",
            (escalation_id,)).fetchall()
        return [ClinicianResponse(**_str(r)) for r in rows]

    @contextmanager
    def as_user(self, auth_user_id: str):
        """Run reads as this Supabase user so RLS decides what they see."""
        claims = json.dumps({"sub": auth_user_id, "role": "authenticated"})
        with self.conn.transaction():
            self.conn.execute("set local role authenticated")
            self.conn.execute("select set_config('request.jwt.claims', %s, true)", (claims,))
            yield self.conn
