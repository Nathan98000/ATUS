"""The in-process data snapshot: what the served database contains (audit F14).

Before Phase 5 every analysis request, cache hits and invalid requests
included, read the data version from the database, and every metadata route
read its answer from it. Each of those reads can wake a scale-to-zero
database. The API now keeps in memory:

* the data version (:func:`atus_pipeline.database.versions.read_data_version`)
  and the latest succeeded ingestion run;
* the loaded survey years and the respondent count (``/meta``);
* the activity lexicon (``/activities``, ``/activities/{code}``) and an
  ``ActivityResolver`` built from it, which every analysis engine reuses
  together with the years instead of reading them again.

All of it is read in one REPEATABLE READ metadata transaction, so the parts
describe the same committed data. The snapshot is read when a request needs
it and it is older than ``ATUS_API_SNAPSHOT_TTL_S``; there is no background
refresh, so an idle API never touches the database (I08). Concurrent
requests that find it stale share one refresh.

While the snapshot is fresh, analysis cache hits, every 4xx, ``/meta``,
``/activities*`` and ``/population/metadata`` cost no database statement.
Uncached analyses still re-read the data version inside their own
transaction after computing (the concurrent-reload guard in
``analysis_runner``); a changed version invalidates the snapshot.

**Deployment binding.** With ``ATUS_EXPECTED_DATA_VERSION`` set, a snapshot
of any other version makes every data route answer 503
``data_version_mismatch`` (ADR-011 release switch): an API pointed at the
wrong database serves nothing from it.

**Lexicon search in Python.** ``GET /activities?search=`` used to run

    name ILIKE '%' || <LIKE-escaped term> || '%' ESCAPE '\\'

For a UTF-8 database, PostgreSQL evaluates ILIKE as a case-sensitive LIKE
between ``lower(name)`` and ``lower(pattern)`` under the column's collation
(``Generic_Text_IC_like`` in ``src/backend/utils/adt/like.c``), and lowering
leaves the escape characters alone, so the condition is exactly "``lower(term)``
is a substring of ``lower(name)``" (a byte-wise substring of valid UTF-8 is a
character-wise one). The snapshot therefore stores each name lowered *by the
database*, and lowers the search term in Python only when that is provably
the database's own result: the term is ASCII and the database lowers every
printable ASCII character exactly as Python does (checked at every refresh;
a Turkish locale, for instance, would fail it). Any other term is searched
with the original SQL. An integration test compares both paths on the
fixture database.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import anyio
import psycopg

from ..analytics.activities import ActivityResolver
from ..database import versions
from .errors import DataVersionMismatchError

log = logging.getLogger("atus.api")

# One flat view over the three lexicon tables. level: 1 = major category (2
# digits), 2 = subcategory (4), 3 = leaf activity (6).
_LEXICON_ROWS = """
    SELECT code, name, 1 AS level, NULL AS parent_code,
           NULL AS harmonization_note
    FROM atus.activity_tier1
    UNION ALL
    SELECT code, name, 2, left(code, 2), NULL FROM atus.activity_tier2
    UNION ALL
    SELECT code, name, 3, tier2_code, harmonization_note FROM atus.activity_codes
"""

LEXICON_SQL = (
    "SELECT code, name, level, parent_code, harmonization_note FROM ("
    + _LEXICON_ROWS + ") lexicon"
)
"""The listing's five columns (the SQL search path filters and orders it)."""

# The rows of the unfiltered listing, in its order, plus each name lowered
# by the database (for the Python search; see module docstring).
_SNAPSHOT_LEXICON_SQL = (
    "SELECT code, name, level, parent_code, harmonization_note, lower(name) FROM ("
    + _LEXICON_ROWS + ") lexicon ORDER BY code, level"
)

_LATEST_RUN_SQL = (
    "SELECT id, release, finished_at FROM atus.ingestion_runs "
    "WHERE status = 'succeeded' ORDER BY id DESC LIMIT 1"
)
# The engine's own loaded-years query (analytics/engine.py), so the years
# handed to engines are exactly the ones it would have read itself.
_YEARS_SQL = "SELECT DISTINCT data_year FROM atus.respondents ORDER BY data_year"
_RESPONDENTS_SQL = "SELECT count(*) FROM atus.respondents"
_LOWER_SQL = "SELECT lower(%s)"

ASCII_PROBE = "".join(chr(c) for c in range(0x20, 0x7F))
"""Every printable ASCII character: the database must lower these exactly
as Python does before a search term is lowered in Python."""

_RELEASE_PATTERN = re.compile(r"^(\d{2})(\d{2})$")


def lexicon_label(release: str | None) -> str | None:
    """``'0325'`` -> ``'2003-25'``: the year span a BLS multi-year release
    code names (ATUS began in 2003, so both years are 20xx). ``None`` for an
    unknown format."""
    match = _RELEASE_PATTERN.match(release or "")
    if match is None:
        return None
    return f"20{match.group(1)}-{match.group(2)}"


@dataclass(frozen=True)
class LexiconEntry:
    code: str
    name: str
    level: int
    parent_code: str | None
    harmonization_note: str | None
    name_lower: str | None = field(default=None, repr=False)  # lowered by the database

    def row(self) -> tuple:
        """The five columns of ``LEXICON_SQL``."""
        return (self.code, self.name, self.level, self.parent_code, self.harmonization_note)


@dataclass(frozen=True)
class IngestionRun:
    id: int
    release: str
    finished_at: datetime | None


@dataclass(frozen=True)
class ActivityDetail:
    entry: LexiconEntry
    parents: tuple[LexiconEntry, ...]
    children: tuple[LexiconEntry, ...]
    descendant_leaf_count: int


@dataclass(frozen=True, eq=False)
class DataSnapshot:
    """An immutable view of the served data (built by :func:`load_snapshot`)."""

    data_version: str
    run: IngestionRun | None
    years: tuple[int, ...]
    respondents: int
    lexicon: tuple[LexiconEntry, ...]       # ORDER BY code, level, as the database sorted it
    resolver: ActivityResolver | None = None
    ascii_lowering_matches: bool = True     # the database lowers ASCII as Python does
    _by_code: dict[str, LexiconEntry] = field(init=False, repr=False)
    _children: dict[str, tuple[LexiconEntry, ...]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        by_code: dict[str, LexiconEntry] = {}
        children: dict[str, list[LexiconEntry]] = {}
        for entry in self.lexicon:
            by_code.setdefault(entry.code, entry)  # first row, as fetchone() returned
            if entry.parent_code is not None:
                children.setdefault(entry.parent_code, []).append(entry)
        object.__setattr__(self, "_by_code", by_code)
        object.__setattr__(
            self, "_children", {code: tuple(rows) for code, rows in children.items()}
        )

    @property
    def is_loaded(self) -> bool:
        return self.data_version != versions.UNLOADED

    @property
    def lexicon_label(self) -> str | None:
        return lexicon_label(self.run.release if self.run else None)

    def engine_kwargs(self) -> dict:
        """What an ``AnalysisEngine`` may reuse instead of reading it again."""
        return {"resolver": self.resolver, "loaded_years": self.years or None}

    def search_lexicon(
        self, *, level: int | None, parent: str | None, search: str | None
    ) -> list[LexiconEntry] | None:
        """The listing endpoint's rows, in its order; ``None`` when the search
        term must go to the database (see module docstring)."""
        if search is not None and not (self.ascii_lowering_matches and search.isascii()):
            return None
        needle = search.lower() if search is not None else None
        return [
            entry for entry in self.lexicon
            if (level is None or entry.level == level)
            and (parent is None or entry.parent_code == parent)
            and (needle is None or (entry.name_lower is not None and needle in entry.name_lower))
        ]

    def detail(self, code: str) -> ActivityDetail | None:
        """One code with its ancestry, children and leaf count, as the
        detail endpoint's SQL returned them; ``None`` if it does not exist."""
        entry = self._by_code.get(code)
        if entry is None:
            return None
        parents = tuple(
            self._by_code[code[:length]]
            for length in (2, 4)
            if length < len(code) and code[:length] in self._by_code
        )
        leaves = sum(
            1 for row in self.lexicon if row.level == 3 and row.code.startswith(code)
        )
        return ActivityDetail(
            entry=entry, parents=parents, children=self._children.get(code, ()),
            descendant_leaf_count=leaves,
        )


def load_snapshot(conn: psycopg.Connection) -> DataSnapshot:
    """Read a snapshot on ``conn`` (the caller supplies the transaction)."""
    data_version = versions.read_data_version(conn)
    run_row = conn.execute(_LATEST_RUN_SQL).fetchone()
    years = tuple(row[0] for row in conn.execute(_YEARS_SQL))
    respondents = conn.execute(_RESPONDENTS_SQL).fetchone()[0]
    lexicon = tuple(
        LexiconEntry(code, name, level, parent, note, lowered)
        for code, name, level, parent, note, lowered in conn.execute(_SNAPSHOT_LEXICON_SQL)
    )
    probe = conn.execute(_LOWER_SQL, (ASCII_PROBE,)).fetchone()[0]
    # The resolver's own lexicon reads; an empty lexicon leaves it to the
    # engine, which reports the unloaded database exactly as before.
    resolver = ActivityResolver(conn) if any(e.level == 3 for e in lexicon) else None
    return DataSnapshot(
        data_version=data_version,
        run=IngestionRun(*run_row) if run_row else None,
        years=years,
        respondents=respondents,
        lexicon=lexicon,
        resolver=resolver,
        ascii_lowering_matches=probe == ASCII_PROBE.lower(),
    )


class SnapshotStore:
    """Holds the current snapshot and refreshes it when a request needs it.

    Built once per application lifespan (inside the event loop). ``get`` is
    the only way routes and the analysis runner obtain data facts.
    """

    def __init__(
        self, ttl_s: float, expected_version: str | None = None,
        *, clock: Callable[[], float] = time.monotonic,
    ):
        self.ttl_s = ttl_s
        self.expected_version = expected_version
        self._clock = clock
        self._lock = threading.Lock()  # invalidate() is called from worker threads
        self._snapshot: DataSnapshot | None = None
        self._fresh_until = -math.inf
        self._loaded_at = -math.inf
        self._pending: asyncio.Future | None = None
        self.refreshes = 0
        self.refresh_failures = 0

    @property
    def current(self) -> DataSnapshot | None:
        """The last snapshot read, fresh or not (``None`` before the first)."""
        return self._snapshot

    def age_s(self) -> float | None:
        if self._snapshot is None:
            return None
        return max(0.0, self._clock() - self._loaded_at)

    def is_fresh(self) -> bool:
        with self._lock:
            return self._snapshot is not None and self._clock() < self._fresh_until

    def invalidate(self) -> None:
        """Make the next request read a new snapshot (thread-safe)."""
        with self._lock:
            self._fresh_until = -math.inf

    def check_expected(self, snapshot: DataSnapshot) -> None:
        if self.expected_version is not None and snapshot.data_version != self.expected_version:
            raise DataVersionMismatchError(
                f"database serves {snapshot.data_version!r}, "
                f"deployment expects {self.expected_version!r}"
            )

    async def get(self, db, *, check_expected: bool = True) -> DataSnapshot:
        """The current snapshot, read first if it is older than the TTL.

        ``db`` is the request's database (anything with ``read_snapshot()``);
        its errors propagate unchanged. Raises
        :class:`~atus_pipeline.api.errors.DataVersionMismatchError` when the
        deployment expects another data version, unless ``check_expected``
        is false (readiness reports the mismatch itself).
        """
        if self.is_fresh():
            snapshot = self._snapshot
        else:
            snapshot = await self._refresh(db)
        if check_expected:
            self.check_expected(snapshot)
        return snapshot

    async def _refresh(self, db) -> DataSnapshot:
        if self._pending is None:
            # Created in the requesting context, so its database work is
            # counted against the request that triggered it.
            self._pending = asyncio.ensure_future(self._load(db))
            self._pending.add_done_callback(self._settle)
        return await asyncio.shield(self._pending)

    async def _load(self, db) -> DataSnapshot:
        snapshot = await anyio.to_thread.run_sync(db.read_snapshot)
        now = self._clock()
        with self._lock:
            self._snapshot = snapshot
            self._loaded_at = now
            self._fresh_until = now + self.ttl_s
        self.refreshes += 1
        if self.expected_version is not None and snapshot.data_version != self.expected_version:
            log.error(
                "data version mismatch: database serves %s, ATUS_EXPECTED_DATA_VERSION is %s",
                snapshot.data_version, self.expected_version,
            )
        return snapshot

    def _settle(self, future: asyncio.Future) -> None:
        self._pending = None
        if future.cancelled():
            return
        if future.exception() is not None:  # retrieved here; each waiter re-raises it
            self.refresh_failures += 1
