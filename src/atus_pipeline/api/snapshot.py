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
describe the same committed data. That transaction locks ``ingestion_runs``
(ACCESS SHARE) before its snapshot is taken (``Database.read_snapshot``), so
a concurrent ``atus load`` switch, which locks that table first, has either
committed before the snapshot or waits until the read ends: the data
version and run are never paired with another version's tables, which the
switch moves in by name. The snapshot is read when a request needs
it; there is no background refresh, so an idle API never touches the database
(I08). Concurrent requests that need a read share one refresh.

While the snapshot is usable, analysis cache hits, every 4xx, ``/meta``,
``/activities*`` and ``/population/metadata`` cost no database statement.
Uncached analyses still read the data version first in their own
transaction (the concurrent-reload guard in ``analysis_runner``); a changed
version invalidates the snapshot.

**When it is read again** (ADR-012, "Metadata reads and the quota"):

* **Pinned.** With ``ATUS_EXPECTED_DATA_VERSION`` set (production), a
  snapshot of exactly that version never expires: the deployment is bound to
  that data, and data only changes with a release switch, which redeploys
  the API. Metadata requests, however many, therefore cost no statement
  after the first read of each process, and cannot keep a scale-to-zero
  database awake. Only ``invalidate()`` (an analysis saw the version change)
  forces a new read.
* **Otherwise** it is read again once it is older than
  ``ATUS_API_SNAPSHOT_TTL_S``.
* **Stale if error.** If a read fails (the database is down, suspended or
  paused) while a previous, never-invalidated snapshot exists, requests are
  served from that snapshot instead of failing, and the database is tried
  again only after ``min(STALE_IF_ERROR_RETRY_S, TTL)``. Cache hits and
  metadata keep working; uncached analyses still need the database and fail
  as before. ``/ready`` (``strict``) uses neither pinning nor staleness.

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
database*, and lowers the search term with the *database's own* lowering,
read once per snapshot: ``lower(chr(c))`` for every code point ``c`` of every
Unicode block that has case mappings (:data:`CASED_RANGES`; every other code
point lowers to itself, which a unit test proves for Python's Unicode data
and an integration test for the database's). Applying that table character
by character equals the database's ``lower(term)`` when the database lowers
text one character at a time, as libc collations (Neon's and the stack's)
and PostgreSQL's builtin provider do. A probe checks this at every read
(:data:`CONTEXT_PROBE`: an ICU collation lowers a final capital sigma by its
context); if it fails, every search term goes to the original SQL. So
non-ASCII terms ("décor") cost no statement either, and a flood of search
requests cannot wake the database. An integration test compares both paths
on the real lexicon for thousands of terms, ASCII and not.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import anyio
import psycopg

from ..analytics.activities import ActivityResolver
from ..database import versions
from ..sources import lexicon_label
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
CASED_RANGES: tuple[tuple[int, int], ...] = (
    (0x0020, 0x007E),     # Basic Latin (printable; a Turkish locale lowers I differently)
    (0x00A0, 0x058F),     # Latin-1 .. Latin Extended-B, IPA, Greek and Coptic, Cyrillic, Armenian
    (0x10A0, 0x10FF),     # Georgian
    (0x13A0, 0x13FF),     # Cherokee
    (0x1C80, 0x1CBF),     # Cyrillic Extended-C, Georgian Extended
    (0x1D00, 0x1FFF),     # Phonetic Extensions .. Latin Extended Additional, Greek Extended
    (0x2100, 0x218F),     # Letterlike Symbols (Kelvin, Angstrom), Number Forms (Roman numerals)
    (0x24B6, 0x24E9),     # circled Latin letters
    (0x2C00, 0x2D2F),     # Glagolitic, Latin Extended-C, Coptic, Georgian Supplement
    (0xA640, 0xA69F),     # Cyrillic Extended-B
    (0xA720, 0xA7FF),     # Latin Extended-D
    (0xAB30, 0xABBF),     # Latin Extended-E, Cherokee Supplement
    (0xFF21, 0xFF5A),     # fullwidth Latin letters
    (0x10400, 0x1044F),   # Deseret
    (0x104B0, 0x104FF),   # Osage
    (0x10570, 0x105BF),   # Vithkuqi
    (0x10C80, 0x10CFF),   # Old Hungarian
    (0x10D40, 0x10D8F),   # Garay (Unicode 16; newer than Python 3.12's data)
    (0x118A0, 0x118FF),   # Warang Citi
    (0x16E40, 0x16E9F),   # Medefaidrin
    (0x1E900, 0x1E95F),   # Adlam
)
"""Every Unicode block with characters whose lowercase differs from
themselves (block-aligned, so characters added to these blocks later are
covered), up to Unicode 16. Every code point outside them lowers to itself
(tests: Python's Unicode data, and the database's own collation). A cased
script added in a later Unicode version lowers into its own new letters,
which no lexicon name contains, so missing it cannot change a result."""

CONTEXT_PROBE = "ΑΣ ΣΑ ΑΣΑ. İI Kİ ǅǄ"
"""Lowered whole and character by character, these agree only when the
database lowers text one character at a time (a final capital sigma is
lowered by its context under ICU)."""

_LOWERING_SQL = """
    SELECT lower(%(probe)s),
           coalesce(array_agg(cp ORDER BY cp), '{}'),
           coalesce(array_agg(lowered ORDER BY cp), '{}')
    FROM (
        SELECT cp, lower(chr(cp)) AS lowered
        FROM unnest(%(lo)s::int[], %(hi)s::int[]) AS r (lo, hi),
             generate_series(r.lo, r.hi) AS cp
    ) chars
    WHERE lowered <> chr(cp)
"""
"""One statement: the probe lowered whole, and the database's lowering of
every code point in :data:`CASED_RANGES` that does not lower to itself."""

STALE_IF_ERROR_RETRY_S = 30.0
"""After a failed read, how long the previous snapshot is served before the
database is tried again (never longer than the TTL)."""


def lowering_table(conn: psycopg.Connection) -> dict[str, str] | None:
    """The database's ``lower()`` of every character that it does not lower
    to itself, or ``None`` when it lowers text context-sensitively (then a
    character table cannot reproduce it). Runs one statement on ``conn``."""
    probe_lower, cps, lowered = conn.execute(_LOWERING_SQL, {
        "probe": CONTEXT_PROBE,
        "lo": [lo for lo, _ in CASED_RANGES],
        "hi": [hi for _, hi in CASED_RANGES],
    }).fetchone()
    table = {chr(cp): low for cp, low in zip(cps, lowered, strict=True)}
    if lower_with(table, CONTEXT_PROBE) != probe_lower:
        log.warning("lexicon search: the database lowers text by context (an ICU "
                    "collation?); search terms are answered by SQL")
        return None
    return table


def lower_with(table: dict[str, str], text: str) -> str:
    """``text`` lowered character by character with ``table`` (characters
    it does not list lower to themselves)."""
    return "".join(table.get(ch, ch) for ch in text)


@functools.cache
def python_lowering() -> dict[str, str]:
    """The table Python's own ``str.lower`` gives for :data:`CASED_RANGES`:
    the lowering of DB-less test snapshots, never of a real one."""
    table = {}
    for lo, hi in CASED_RANGES:
        for cp in range(lo, hi + 1):
            ch = chr(cp)
            if ch.lower() != ch:
                table[ch] = ch.lower()
    return table

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
    # The database's character lowering (``lowering_table``); ``None``: the
    # database lowers by context, so searches go to SQL.
    lowering: dict[str, str] | None = field(default_factory=python_lowering, repr=False)
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
        if search is None:
            needle = None
        elif self.lowering is None:
            return None
        else:
            needle = lower_with(self.lowering, search)
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
    lowering = lowering_table(conn)
    # The resolver's own lexicon reads; an empty lexicon leaves it to the
    # engine, which reports the unloaded database exactly as before.
    resolver = (
        ActivityResolver(conn, release=run_row[1] if run_row else None)
        if any(e.level == 3 for e in lexicon) else None
    )
    return DataSnapshot(
        data_version=data_version,
        run=IngestionRun(*run_row) if run_row else None,
        years=years,
        respondents=respondents,
        lexicon=lexicon,
        resolver=resolver,
        lowering=lowering,
    )


class SnapshotStore:
    """Holds the current snapshot and reads it again when a request needs it
    (see the module docstring: pinned, TTL, stale if error).

    Built once per application lifespan (inside the event loop). ``get`` is
    the only way routes and the analysis runner obtain data facts.
    """

    def __init__(
        self, ttl_s: float, expected_version: str | None = None,
        *, clock: Callable[[], float] = time.monotonic,
        stale_retry_s: float = STALE_IF_ERROR_RETRY_S,
    ):
        self.ttl_s = ttl_s
        self.expected_version = expected_version
        self.stale_retry_s = min(stale_retry_s, ttl_s)
        self._clock = clock
        self._lock = threading.Lock()  # invalidate() is called from worker threads
        self._snapshot: DataSnapshot | None = None
        # The snapshot was read without an invalidate() since it started:
        # only then may it be pinned or served stale.
        self._trusted = False
        self._fresh_until = -math.inf
        self._stale_until = -math.inf  # after a failed read: serve the old one until then
        self._loaded_at = -math.inf
        self._generation = 0  # bumped by invalidate()
        self._pending: asyncio.Future | None = None
        self.refreshes = 0
        self.refresh_failures = 0
        self.stale_serves = 0

    @property
    def current(self) -> DataSnapshot | None:
        """The last snapshot read, fresh or not (``None`` before the first)."""
        return self._snapshot

    def age_s(self) -> float | None:
        if self._snapshot is None:
            return None
        return max(0.0, self._clock() - self._loaded_at)

    def is_fresh(self) -> bool:
        """Read within the TTL and not invalidated since."""
        with self._lock:
            return self._snapshot is not None and self._clock() < self._fresh_until

    def is_pinned(self) -> bool:
        """The snapshot is of exactly the version the deployment expects and
        was never invalidated: it is used without reading again."""
        with self._lock:
            return self._pinned()

    def _pinned(self) -> bool:
        return (
            self._trusted
            and self.expected_version is not None
            and self._snapshot is not None
            and self._snapshot.data_version == self.expected_version
        )

    def invalidate(self) -> None:
        """Make the next request read a new snapshot (thread-safe). A read
        already in progress may predate the change that prompted this, so
        its result is used by the requests waiting for it but not kept as
        fresh, pinned or a stale fallback."""
        with self._lock:
            self._fresh_until = -math.inf
            self._stale_until = -math.inf
            self._trusted = False
            self._generation += 1

    def check_expected(self, snapshot: DataSnapshot) -> None:
        if self.expected_version is not None and snapshot.data_version != self.expected_version:
            raise DataVersionMismatchError(
                f"database serves {snapshot.data_version!r}, "
                f"deployment expects {self.expected_version!r}"
            )

    async def get(
        self, db, *, check_expected: bool = True, strict: bool = False
    ) -> DataSnapshot:
        """The current snapshot, read first when it is not usable as is.

        ``db`` is the request's database (anything with ``read_snapshot()``).
        Raises :class:`~atus_pipeline.api.errors.DataVersionMismatchError`
        when the deployment expects another data version, unless
        ``check_expected`` is false (readiness reports the mismatch itself).
        A failed read raises the database's error unchanged, unless a
        previous trusted snapshot can be served instead (stale if error).
        ``strict`` (readiness) uses the TTL alone: no pinning, no stale
        snapshot, so the answer reflects the database as of a read at most
        ``ttl_s`` old (``age_s()`` says how old).
        """
        snapshot = self._usable(strict)
        if snapshot is None:
            try:
                snapshot = await self._refresh(db)
            except Exception:
                snapshot = None if strict else self._stale_fallback()
                if snapshot is None:
                    raise
        if check_expected:
            self.check_expected(snapshot)
        return snapshot

    def _usable(self, strict: bool) -> DataSnapshot | None:
        with self._lock:
            snapshot = self._snapshot
            if snapshot is None:
                return None
            now = self._clock()
            if now < self._fresh_until:
                return snapshot
            if strict or not self._trusted:
                return None
            if self._pinned():
                return snapshot
            if now < self._stale_until:  # a read failed recently: do not retry yet
                self.stale_serves += 1
                return snapshot
            return None

    def _stale_fallback(self) -> DataSnapshot | None:
        with self._lock:
            if self._snapshot is None or not self._trusted:
                return None
            self.stale_serves += 1
            return self._snapshot

    async def _refresh(self, db) -> DataSnapshot:
        if self._pending is None:
            # Created in the requesting context, so its database work is
            # counted against the request that triggered it.
            self._pending = asyncio.ensure_future(self._load(db))
            self._pending.add_done_callback(self._settle)
        return await asyncio.shield(self._pending)

    async def _load(self, db) -> DataSnapshot:
        with self._lock:
            generation = self._generation
        snapshot = await anyio.to_thread.run_sync(db.read_snapshot)
        now = self._clock()
        with self._lock:
            self._snapshot = snapshot
            self._loaded_at = now
            self._stale_until = -math.inf
            # Invalidated while reading (a computation saw the data version
            # change): this read may have seen the old data, so the next
            # request reads again instead of trusting it.
            self._trusted = self._generation == generation
            if self._trusted:
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
        error = future.exception()  # retrieved here; each waiter re-raises it
        if error is None:
            return
        self.refresh_failures += 1
        with self._lock:
            if self._snapshot is not None and self._trusted:
                self._stale_until = self._clock() + self.stale_retry_s
                log.warning(
                    "data snapshot read failed (%s); serving the snapshot read %.0f s ago "
                    "and retrying in %.0f s", type(error).__name__,
                    max(0.0, self._clock() - self._loaded_at), self.stale_retry_s,
                )
