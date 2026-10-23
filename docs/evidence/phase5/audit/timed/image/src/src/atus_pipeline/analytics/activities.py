"""Activity-selector resolution against the loaded BLS coding lexicon.

The harmonized 2003-25 lexicon is strictly prefix-hierarchical (tier1 = first
2 digits, tier2 = first 4, enforced by Phase 1 schema constraints), so "a
category and all its descendants" is exactly a code-prefix match. Resolution
validates every requested code against ``atus.activity_tier1/2`` /
``atus.activity_codes`` and produces the parameterized SQL fragment used to
aggregate episode minutes.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from .errors import InvalidSpecError, UnknownActivityError
from .spec import ActivitySelector


@dataclass(frozen=True)
class ResolvedActivity:
    """A validated selector plus everything needed for querying/reporting."""

    selector: ActivitySelector
    label: str
    leaf_codes: tuple[str, ...]     # all matching 6-digit codes
    sql_condition: str              # references atus.activities AS a; uses %(...)s params
    sql_params: dict[str, object]

    @property
    def description(self) -> str:
        include = "+".join(self.selector.include)
        exclude = f" minus {'+'.join(self.selector.exclude)}" if self.selector.exclude else ""
        return f"{self.label} [{include}{exclude}]"


class ActivityResolver:
    """Validates selectors against the lexicon (loaded once per resolver)."""

    def __init__(self, conn: psycopg.Connection):
        self._tier1 = {row[0] for row in conn.execute("SELECT code FROM atus.activity_tier1")}
        self._tier2 = {row[0] for row in conn.execute("SELECT code FROM atus.activity_tier2")}
        rows = conn.execute("SELECT code, name FROM atus.activity_codes").fetchall()
        self._codes = {code: name for code, name in rows}
        if not self._codes:
            raise InvalidSpecError(
                "The activity lexicon is not loaded — the ATUS database has "
                "not been populated yet."
            )

    def _check_code(self, code: str) -> None:
        table = {2: self._tier1, 4: self._tier2, 6: set(self._codes)}[len(code)]
        if code not in table:
            tier = {2: "tier-1", 4: "tier-2", 6: "6-digit"}[len(code)]
            raise UnknownActivityError(
                f"Activity code {code!r} is not a {tier} code in the 2003-25 "
                f"harmonized activity lexicon."
            )

    def resolve(self, selector: ActivitySelector) -> ResolvedActivity:
        for code in (*selector.include, *selector.exclude):
            self._check_code(code)

        def matches(leaf: str, prefixes: tuple[str, ...]) -> bool:
            return any(leaf.startswith(p) for p in prefixes)

        leaves = tuple(
            sorted(
                leaf for leaf in self._codes
                if matches(leaf, selector.include) and not matches(leaf, selector.exclude)
            )
        )
        if not leaves:
            raise InvalidSpecError(
                f"Activity selection include={selector.include} "
                f"exclude={selector.exclude} matches no activity codes."
            )

        label = selector.label or (
            self._codes[leaves[0]] if len(leaves) == 1 else f"{len(leaves)} activity codes"
        )
        # Match on the 6-digit leaf set: exact, index-friendly, and immune to
        # include/exclude precedence subtleties.
        condition = "a.activity_code = ANY(%(activity_codes)s)"
        return ResolvedActivity(
            selector=selector,
            label=label,
            leaf_codes=leaves,
            sql_condition=condition,
            sql_params={"activity_codes": list(leaves)},
        )
