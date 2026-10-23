"""Request-level analysis-result cache.

Analytical results are deterministic functions of (operation, canonical
analysis specification, analytics version, data version), so those four things
*are* the cache key — versioning is the invalidation mechanism, not TTLs. The
data version is ``<release>:run<latest successful ingestion run id>``, which
changes whenever `atus load` rebuilds the database.

The default backend is an in-process, thread-safe LRU (the API currently runs
as a single process in development; see docs/api.md). The ``AnalysisCache``
protocol keeps the backend replaceable (e.g. Redis in a multi-process
deployment) without touching route code. Only complete, successful result
payloads are stored — errors are never cached, and a cache hit returns the
byte-identical payload the miss produced, warnings included.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from typing import Protocol


def cache_key(
    operation: str, spec_payload: dict, analytics_version: str, data_version: str
) -> str:
    """Deterministic key for one analysis.

    ``spec_payload`` must already be in canonical form (the request adapters
    sort order-insensitive collections such as years and activity code lists);
    serialization here is order-stable (sorted keys, compact separators).
    """
    canonical = json.dumps(
        {
            "operation": operation,
            "spec": spec_payload,
            "analytics_version": analytics_version,
            "data_version": data_version,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class AnalysisCache(Protocol):
    def get(self, key: str) -> dict | None: ...
    def put(self, key: str, value: dict) -> None: ...


class InMemoryAnalysisCache:
    """Thread-safe LRU over complete result payloads."""

    def __init__(self, maxsize: int = 256):
        if maxsize < 1:
            raise ValueError("cache maxsize must be >= 1")
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, dict] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> dict | None:
        with self._lock:
            value = self._entries.get(key)
            if value is None:
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return value

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self._maxsize:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class NullAnalysisCache:
    """No-op cache: correctness without caching (graceful degradation)."""

    def get(self, key: str) -> dict | None:
        return None

    def put(self, key: str, value: dict) -> None:
        return None
