"""Living memory: structured facts that change over time without losing history.

Extractors (rules or LLM) never write facts. They propose operations:

    {"op": "add",           "kind": "medication", "value": "Advil", "status": "active"}
    {"op": "update_status", "target_id": "...", "status": "stopped", "detail": {"effective": "last week"}}
    {"op": "correct_value", "target_id": "...", "value": "Panadol"}
    {"op": "retract",       "target_id": "..."}

`apply_operations` validates each one and appends rows. Rows are never edited:
a change is a new row whose `supersedes_id` points at the row it replaces, so
following that chain backwards gives the full history, each step linked to the
message that caused it. This mirrors the `memory_item` table in Supabase.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.logs import log_event
from app.redaction import redact

ALLOWED_STATUS = {
    "chief_complaint": {"active", "resolved"},
    "symptom": {"active", "resolved"},
    "medication": {"active", "stopped"},
    "allergy": {"active", "denied"},
    "condition": {"active", "resolved"},
    "procedure": {"active", "resolved"},
    "goal": {"active", "resolved"},
    "other": {"active", "resolved"},
}
ANY_KIND_STATUS = {"retracted"}  # "ignore what I said" works for every kind
NO_KNOWN_ALLERGIES = "no known allergies"
MAX_VALUE_LEN = 120


@dataclass(frozen=True)
class MemoryItem:
    id: str
    kind: str
    value: str
    status: str
    provenance_message_id: str
    updated_at: str
    detail: dict = field(default_factory=dict)
    supersedes_id: str | None = None
    conflict_with_id: str | None = None
    extracted_by: str = "llm"


class InMemoryStore:
    """Append-only store. The Supabase-backed store will have the same methods."""

    def __init__(self):
        self._items: list[MemoryItem] = []

    def append(self, item: MemoryItem) -> MemoryItem:
        self._items.append(item)
        return item

    def all(self) -> list[MemoryItem]:
        return list(self._items)

    def get(self, item_id: str) -> MemoryItem | None:
        return next((i for i in self._items if i.id == item_id), None)

    def current(self) -> list[MemoryItem]:
        replaced = {i.supersedes_id for i in self._items if i.supersedes_id}
        return [i for i in self._items if i.id not in replaced]


@dataclass
class OpResult:
    op: dict
    applied: bool
    reason: str = ""
    item: MemoryItem | None = None


def _norm(value: str) -> str:
    return " ".join(value.lower().split())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_value(value) -> str | None:
    """Return a rejection reason, or None if the value is fine to store."""
    if not isinstance(value, str) or not value.strip():
        return "empty value"
    if len(value) > MAX_VALUE_LEN:
        return "value too long"
    # Memory holds clinical facts, never identifiers
    result = redact(value)
    if not result.ok or result.entities or "[REDACTED]" in value:
        return "value contains an identifier"
    return None


def _check_status(kind: str, status) -> str | None:
    if status in ANY_KIND_STATUS or status in ALLOWED_STATUS.get(kind, set()):
        return None
    return f"status {status!r} not allowed for {kind}"


def _new(store, *, kind, value, status, message_id, extracted_by, detail=None,
         supersedes=None, conflict_with=None) -> MemoryItem:
    return store.append(MemoryItem(
        id=str(uuid.uuid4()),
        kind=kind,
        value=value.strip(),
        status=status,
        provenance_message_id=message_id,
        updated_at=_now(),
        detail=dict(detail or {}),
        supersedes_id=supersedes.id if supersedes else None,
        conflict_with_id=conflict_with.id if conflict_with else None,
        extracted_by=extracted_by,
    ))


def _apply_add(store, op, message_id, extracted_by) -> OpResult:
    kind, value, status = op.get("kind"), op.get("value"), op.get("status", "active")
    if kind not in ALLOWED_STATUS:
        return OpResult(op, False, f"unknown kind {kind!r}")
    if reason := _check_value(value) or _check_status(kind, status):
        return OpResult(op, False, reason)

    current = store.current()
    same = [i for i in current if i.kind == kind and _norm(i.value) == _norm(value)]
    if any(i.status == status for i in same):
        return OpResult(op, False, "already known")

    # "No allergies" while allergies are on file, or "I take X" after X was stopped:
    # don't guess which is true. Keep both and flag it for a clinician.
    conflict = same[0] if same else None
    if kind == "allergy" and _norm(value) == NO_KNOWN_ALLERGIES:
        conflict = next((i for i in current if i.kind == "allergy" and i.status == "active"
                         and _norm(i.value) != NO_KNOWN_ALLERGIES), conflict)
    elif kind == "allergy":
        conflict = next((i for i in current if i.kind == "allergy"
                         and _norm(i.value) == NO_KNOWN_ALLERGIES), conflict)

    item = _new(store, kind=kind, value=value, status=status, message_id=message_id,
                extracted_by=extracted_by, detail=op.get("detail"), conflict_with=conflict)
    return OpResult(op, True, "conflict flagged" if conflict else "", item)


def _apply_change(store, op, message_id, extracted_by) -> OpResult:
    target = store.get(op.get("target_id") or "")
    if target is None:
        return OpResult(op, False, "unknown target")
    if target not in store.current():
        return OpResult(op, False, "target already superseded")

    value, status = target.value, target.status
    if op["op"] == "update_status":
        status = op.get("status")
    elif op["op"] == "correct_value":
        value = op.get("value")
    else:  # retract
        status = "retracted"

    if reason := _check_value(value) or _check_status(target.kind, status):
        return OpResult(op, False, reason)
    if value == target.value and status == target.status:
        return OpResult(op, False, "no change")

    item = _new(store, kind=target.kind, value=value, status=status, message_id=message_id,
                extracted_by=extracted_by, detail={**target.detail, **(op.get("detail") or {})},
                supersedes=target)
    return OpResult(op, True, "", item)


HANDLERS = {
    "add": _apply_add,
    "update_status": _apply_change,
    "correct_value": _apply_change,
    "retract": _apply_change,
}


def apply_operations(store, ops: list[dict], message_id: str, extracted_by: str = "llm") -> list[OpResult]:
    results = []
    for op in ops:
        handler = HANDLERS.get(op.get("op") if isinstance(op, dict) else None)
        if handler is None:
            results.append(OpResult(op, False, "unknown operation"))
            continue
        results.append(handler(store, op, message_id, extracted_by))

    log_event("memory_updated", message_id=message_id, extracted_by=extracted_by,
              applied=sum(r.applied for r in results),
              rejected=[r.reason for r in results if not r.applied])
    return results


def history(store, item_id: str) -> list[MemoryItem]:
    """Oldest-first chain of versions ending at `item_id`."""
    chain, item = [], store.get(item_id)
    while item:
        chain.append(item)
        item = store.get(item.supersedes_id) if item.supersedes_id else None
    return list(reversed(chain))


def profile(store) -> dict[str, list[dict]]:
    """The live Patient Profile: current facts grouped by kind, with provenance."""
    out: dict[str, list[dict]] = {}
    for item in store.current():
        if item.status == "retracted":
            continue
        out.setdefault(item.kind, []).append({
            "id": item.id,
            "value": item.value,
            "status": item.status,
            "detail": item.detail,
            "provenance_message_id": item.provenance_message_id,
            "updated_at": item.updated_at,
            "conflict_with_id": item.conflict_with_id,
            "versions": len(history(store, item.id)),
        })
    return out
