"""User-facing lexicon text names the loaded release's lexicon (audit F23).

For release 0325 the text must stay byte-identical to what Phases 2-4
served ("2003-25"); for another release it follows that release.
"""

from __future__ import annotations

import pytest

from atus_pipeline.analytics.activities import ActivityResolver
from atus_pipeline.analytics.engine import harmonization_note
from atus_pipeline.analytics.errors import UnknownActivityError
from atus_pipeline.analytics.spec import ActivitySelector

PHASE4_NOTE = (
    "Activity codes use the BLS-harmonized 2003-25 multi-year lexicon "
    "(cross-year comparable by construction)."
)


class LexiconConnection:
    """Answers the resolver's four lexicon/release reads."""

    def __init__(self, release: str | None):
        self.release = release

    def execute(self, sql, params=None):
        if "activity_tier1" in sql:
            rows = [("01",)]
        elif "activity_tier2" in sql:
            rows = [("0101",)]
        elif "activity_codes" in sql:
            rows = [("010101", "Sleeping")]
        elif "ingestion_runs" in sql:
            rows = [({"id": 3, "release": self.release},)] if self.release else []
        else:
            raise AssertionError(sql)

        class Cursor:
            def __iter__(self):
                return iter(rows)

            def fetchall(self):
                return rows

            def fetchone(self):
                return rows[0] if rows else None

        return Cursor()


def test_release_0325_note_is_unchanged():
    assert harmonization_note("2003-25") == PHASE4_NOTE


def test_note_follows_the_release():
    assert "BLS-harmonized 2003-26 multi-year lexicon" in harmonization_note("2003-26")


def test_note_without_a_known_release_is_neutral():
    assert harmonization_note(None) == (
        "Activity codes use the BLS-harmonized multi-year lexicon "
        "(cross-year comparable by construction)."
    )


@pytest.mark.parametrize(
    ("release", "label", "fragment"),
    [
        ("0325", "2003-25", "in the 2003-25 harmonized activity lexicon."),
        ("0326", "2003-26", "in the 2003-26 harmonized activity lexicon."),
        (None, None, "in the harmonized activity lexicon."),
    ],
)
def test_resolver_reads_the_loaded_release(release, label, fragment):
    resolver = ActivityResolver(LexiconConnection(release))
    assert resolver.lexicon_label == label
    with pytest.raises(UnknownActivityError) as info:
        resolver.resolve(ActivitySelector(("019999",)))
    assert str(info.value) == f"Activity code '019999' is not a 6-digit code {fragment}"
