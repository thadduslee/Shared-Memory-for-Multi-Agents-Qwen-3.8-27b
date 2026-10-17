"""Process-local query result cache.

Safety does not depend on invalidation reaching every process: a cache hit is
only served after the service re-verifies, against the database, that every
approved memory is still active, current and authorized for this requester
(the same live re-check used at release time). Invalidation on writes is an
optimization that also restores recall promptly.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class _Entry[T]:
    value: T
    expires_at: datetime
    generation: int


class QueryCache[T]:
    def __init__(self, ttl_seconds: int = 30, max_entries: int = 10_000) -> None:
        self.ttl = timedelta(seconds=ttl_seconds)
        self.max_entries = max_entries
        self._entries: dict[tuple[str, str], _Entry[T]] = {}
        self._generation: dict[str, int] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def generation(self, tenant_id: str) -> int:
        with self._lock:
            return self._generation.get(tenant_id, 0)

    def get(self, tenant_id: str, key: str, now: datetime) -> T | None:
        if self.ttl.total_seconds() <= 0:
            return None
        with self._lock:
            entry = self._entries.get((tenant_id, key))
            if entry is None or entry.expires_at <= now or entry.generation != self._generation.get(tenant_id, 0):
                self.misses += 1
                return None
            self.hits += 1
            return entry.value

    def put(self, tenant_id: str, key: str, value: T, now: datetime, *, not_after: datetime | None = None) -> None:
        if self.ttl.total_seconds() <= 0:
            return
        expires = now + self.ttl
        if not_after is not None and not_after < expires:
            expires = not_after
        with self._lock:
            if len(self._entries) >= self.max_entries:
                self._entries.clear()
            self._entries[(tenant_id, key)] = _Entry(value, expires, self._generation.get(tenant_id, 0))

    def invalidate_tenant(self, tenant_id: str) -> int:
        with self._lock:
            self._generation[tenant_id] = self._generation.get(tenant_id, 0) + 1
            doomed = [k for k in self._entries if k[0] == tenant_id]
            for k in doomed:
                del self._entries[k]
            self.evictions += len(doomed)
            return len(doomed)
