"""Sliding-window rate limits, kept in this process's memory.

Guests are anonymous and free, so they're the abuse surface (bots, LLM-cost
attacks). Limits are per Supabase user id. With more than one server process,
this moves to Redis or a Postgres table; the interface stays the same.
"""

import time
from collections import defaultdict, deque

LIMITS = {
    # (max requests, window seconds)
    "guest_message": [(10, 60), (100, 86_400)],
    "patient_message": [(30, 60), (500, 86_400)],
    "session_create": [(10, 3_600)],
    "summary_email": [(3, 3_600)],      # emails go to addresses we can't verify yet: keep it small
}


class RateLimited(Exception):
    def __init__(self, retry_after: int):
        super().__init__(f"retry after {retry_after}s")
        self.retry_after = retry_after


class RateLimiter:
    def __init__(self, limits: dict | None = None, clock=time.monotonic):
        self._limits = limits or LIMITS
        self._clock = clock
        self._hits: dict[tuple[str, str], deque] = defaultdict(deque)

    def check(self, kind: str, key: str) -> None:
        now = self._clock()
        hits = self._hits[(kind, key)]
        longest = max(window for _, window in self._limits[kind])
        while hits and now - hits[0] > longest:
            hits.popleft()
        for limit, window in self._limits[kind]:
            recent = [t for t in hits if now - t <= window]
            if len(recent) >= limit:
                raise RateLimited(int(window - (now - recent[0])) + 1)
        hits.append(now)
