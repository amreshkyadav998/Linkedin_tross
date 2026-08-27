"""In-memory TTL cache and per-IP rate limiter.

Deliberately simple: a dict and a deque of timestamps. That is enough for a
single free-tier instance, and it means no Redis to provision. If this ever
runs on more than one instance, swap both for a shared store.
"""

from __future__ import annotations

import time
from collections import deque


class TTLCache:
    """Caches profile responses so repeat lookups do not hit LinkedIn."""

    def __init__(self, ttl_seconds: int, max_entries: int = 500):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._store: dict[str, tuple[float, dict]] = {}

    def get(self, key: str) -> dict | None:
        if self.ttl <= 0:
            return None
        entry = self._store.get(key)
        if not entry:
            return None
        expires_at, value = entry
        if expires_at < time.time():
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: dict) -> None:
        if self.ttl <= 0:
            return
        if len(self._store) >= self.max_entries:
            self._evict()
        self._store[key] = (time.time() + self.ttl, value)

    def _evict(self) -> None:
        now = time.time()
        expired = [k for k, (exp, _) in self._store.items() if exp < now]
        for key in expired:
            self._store.pop(key, None)
        # Still full? Drop the oldest quarter.
        if len(self._store) >= self.max_entries:
            oldest = sorted(self._store.items(), key=lambda kv: kv[1][0])
            for key, _ in oldest[: max(1, self.max_entries // 4)]:
                self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    @property
    def size(self) -> int:
        return len(self._store)


class RateLimiter:
    """Fixed 60-second sliding window per client IP."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = {}

    def check(self, client_id: str) -> bool:
        """True if the request is allowed."""
        if self.per_minute <= 0:
            return True

        now = time.time()
        window = self._hits.setdefault(client_id, deque())
        while window and now - window[0] > 60:
            window.popleft()

        if len(window) >= self.per_minute:
            return False

        window.append(now)
        return True

    def retry_after(self, client_id: str) -> int:
        window = self._hits.get(client_id)
        if not window:
            return 1
        return max(1, int(60 - (time.time() - window[0])) + 1)
