"""Repository backed by the Supabase Postgres tables (see supabase/migrations).

The backend connects as the database owner, which bypasses RLS: that's the
"only the backend writes" half of the design. Reads on behalf of a user will
go through `as_user()` so RLS applies to them (used by the API layer).

The repo never commits. The caller owns the transaction: one per request in
the API, and a rolled-back one in the integration test.
"""

import json
import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.memory import MemoryItem
from app.repository import LeadSession, StoredMessage

LEAD_FIELDS = ["id", "clinic_id", "source_channel", "identity_level", "campaign_id", "creative",
               "landing_page", "referral_topic", "guest_auth_user_id", "patient_id", "stage",
               "landing_timestamp", "expires_at", "purged_at"]
MESSAGE_FIELDS = ["id", "lead_session_id", "sender", "sent_as", "content_encrypted", "content_redacted",
                  "redaction_ok", "redaction_entities", "risk_level", "risk_reason", "risk_confidence",
                  "risk_source", "risk_assessed_at", "risk_rule_codes", "intents", "escalation_required",
                  "reply_confidence", "advice_generated", "in_reply_to", "created_at"]
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
        try:
            row = self.conn.execute(
                f"select {', '.join(LEAD_FIELDS)} from public.lead_session where id = %s",
                (lead_session_id,)).fetchone()
        except psycopg.errors.InvalidTextRepresentation:  # not a uuid
            self.conn.rollback()
            return None
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

    @contextmanager
    def as_user(self, auth_user_id: str):
        """Run reads as this Supabase user so RLS decides what they see."""
        claims = json.dumps({"sub": auth_user_id, "role": "authenticated"})
        with self.conn.transaction():
            self.conn.execute("set local role authenticated")
            self.conn.execute("select set_config('request.jwt.claims', %s, true)", (claims,))
            yield self.conn
