"""Structured, PHI-free logging.

Every log line is one JSON object. Callers pass IDs, counts, codes and hashes,
never message text. As a last line of defence, any string field longer than
MAX_FIELD_LEN is dropped, because free text is how PHI leaks into logs.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("nightingale")

MAX_FIELD_LEN = 64


def fingerprint(value: str) -> str:
    """Stable short hash so logs can correlate a value without containing it."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def log_event(event: str, **fields) -> dict:
    record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event}
    for key, value in fields.items():
        if isinstance(value, str) and len(value) > MAX_FIELD_LEN:
            record[key] = "[dropped:too_long]"
        else:
            record[key] = value
    logger.info(json.dumps(record, default=str))
    return record
