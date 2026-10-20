"""Declarative registry of official BLS ATUS source files.

Each multi-year "release" (e.g. ``0325`` = survey years 2003-2025) is described
by a :class:`Release`: its survey-year span plus a tuple of :class:`SourceFile`
entries. Everything the pipeline knows about a source file lives here: its
official URL, the exact archive member holding the data, the expected header,
and the official record count published by BLS in the ``*_info.txt`` file
inside each archive.

Adding a future release (e.g. ``0326``) means adding one
:func:`multiyear_release` entry to the registry with the new row counts (plus
the release's committed lexicon CSV) and, if BLS changes any file layout,
updating the header constants. Everything else that is release-specific (file
names, expected years, lexicon path and label) derives from the entry.

Sources:
    https://www.bls.gov/tus/data/datafiles-0325.htm
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

_BLS_DATAFILES = "https://www.bls.gov/tus/datafiles"

# Exact headers of the 2003-25 multi-year files, in file order. These are used
# for strict file-level validation: if BLS changes a layout in a future
# release, validation fails loudly instead of silently mis-parsing.
RESPONDENT_COLUMNS: tuple[str, ...] = (
    "TUCASEID", "TULINENO", "TESPUHRS", "TRDTIND1", "TRDTOCC1", "TRERNHLY", "TRERNUPD",
    "TRHERNAL", "TRHHCHILD", "TRIMIND1", "TRMJIND1", "TRMJOCC1", "TRMJOCGR", "TRNHHCHILD",
    "TRNUMHOU", "TROHHCHILD", "TRTALONE", "TRTCC", "TRTHHFAMILY", "TRTNOCHILD", "TRTNOHH",
    "TRTO", "TRTOHH", "TRTOHHCHILD", "TRTONHH", "TRTONHHCHILD", "TRTSPONLY", "TRTSPOUSE",
    "TRTUNMPART", "TRWERNAL", "TTHR", "TTOT", "TTWK", "TUABSOT", "TUYEAR", "TEABSRSN",
    "TEERN", "TEERNH1O", "TEERNH2", "TEERNHRO", "TEERNHRY", "TEERNPER", "TEERNRT",
    "TEERNUOT", "TEERNWKP", "TEHRFTPT", "TEHRUSL1", "TEHRUSL2", "TEIO1COW", "TEIO1ICD",
    "TEIO1OCD", "TELAYAVL", "TELAYLK", "TELKAVL", "TELKM1", "TERET1", "TESCHFT", "TUBUS",
    "TUBUS1", "TUBUS2OT", "TUBUSL1", "TUBUSL2", "TUBUSL3", "TUBUSL4", "TUCC2", "TUCC4",
    "TUFWK", "TUIO1MFG", "TUIODP1", "TUIODP2", "TUIODP3", "TULAY", "TULAY6M", "TULAYAVR",
    "TULAYDT", "TULK", "TULKAVR", "TULKDK1", "TULKDK2", "TULKDK3", "TULKDK4", "TULKM2",
    "TULKM3", "TULKM4", "TULKM5", "TULKM6", "TULKPS1", "TULKPS2", "TULKPS3", "TULKPS4",
    "TUMONTH", "TRTCCC", "TRTCCTOT", "TRTCHILD", "TRTCOC", "TRTFAMILY", "TRTFRIEND",
    "TRTHH", "TUDIS2", "TURETOT", "TUSPABS", "TUSPUSFT", "TUSPWK", "TREMODR", "TUCC9",
    "TUDIARYDATE", "TUDIS", "TUDIS1", "TRCHILDNUM", "TUDIARYDAY", "TRERNWA", "TRHOLIDAY",
    "TRSPFTPT", "TRSPPRES", "TRDPFTPT", "TUFNWGTP", "TESPEMPNOT", "TESCHLVL", "TESCHENR",
    "TEMJOT", "TELFS", "TEHRUSLT", "TRYHHCHILD", "TRWBMODR", "TRTALONE_WK", "TRTCCC_WK",
    "TRLVMODR", "TRTEC", "TUECYTD", "TUELDER", "TUELFREQ", "TUELNUM", "TU20FWGT",
)

ROSTER_COLUMNS: tuple[str, ...] = ("TUCASEID", "TULINENO", "TERRP", "TEAGE", "TESEX")

ACTIVITY_COLUMNS: tuple[str, ...] = (
    "TUCASEID", "TUACTIVITY_N", "TUACTDUR24", "TUCC5", "TUCC5B", "TRTCCTOT_LN", "TRTCC_LN",
    "TRTCOC_LN", "TUSTARTTIM", "TUSTOPTIME", "TRCODEP", "TRTIER1P", "TRTIER2P", "TUCC8",
    "TUCUMDUR", "TUCUMDUR24", "TUACTDUR", "TR_03CC57", "TRTO_LN", "TRTONHH_LN", "TRTOHH_LN",
    "TRTHH_LN", "TRTNOHH_LN", "TEWHERE", "TUCC7", "TRWBELIG", "TRTEC_LN", "TUEC24",
    "TUDURSTOP",
)

WHO_COLUMNS: tuple[str, ...] = ("TUCASEID", "TULINENO", "TUACTIVITY_N", "TRWHONA", "TUWHO_CODE")

REPLICATE_WEIGHT_COLUMNS: tuple[str, ...] = (
    "TUCASEID", *[f"TUFNWGTP{i:03d}" for i in range(1, 161)],
)

PANDEMIC_WEIGHT_COLUMNS: tuple[str, ...] = (
    "TUCASEID", *[f"TU20FWGT{i:03d}" for i in range(1, 161)],
)

# The ATUS-CPS file has 265 columns; we validate the count plus the presence of
# the columns the transformation actually reads (see transformation/tables.py).
CPS_REQUIRED_COLUMNS: tuple[str, ...] = (
    "TUCASEID", "TULINENO", "HRYEAR4", "HRMONTH", "GEREG", "GEDIV", "GESTFIPS",
    "GEMETSTA", "GTMETSTA", "HRNUMHOU", "HRHTYPE", "HETENURE", "HUFAMINC", "HEFAMINC",
    "PERRP", "PRTAGE", "PESEX", "PEEDUCA", "PTDTRACE", "PEHSPNON", "PEMARITL",
    "PRCITSHP", "PEMLR",
)


@dataclass(frozen=True)
class SourceFile:
    """One official BLS data file within a release."""

    key: str                 # short pipeline identifier, e.g. "respondent"
    description: str
    url: str
    zip_name: str
    dat_name: str            # CSV data member inside the zip (BLS names them .dat)
    expected_rows: int       # official record count from the BLS *_info.txt
    # Exactly one of the following two is used for header validation:
    expected_columns: tuple[str, ...] | None = None   # full header, in order
    expected_column_count: int | None = None          # for very wide files
    required_columns: tuple[str, ...] = field(default=())
    load: bool = True        # False = staged for validation only, not loaded into the DB
    notes: str = ""


@dataclass(frozen=True)
class Release:
    """One BLS multi-year release: its survey-year span and its source files.

    Everything release-specific in the pipeline derives from this entry: file
    names and URLs, the expected survey years (source and database
    validation), the committed lexicon file, and the lexicon label shown in
    analysis warnings and errors (e.g. "2003-25").
    """

    code: str                        # BLS release code, e.g. "0325" = 2003-2025
    first_year: int
    last_year: int
    files: tuple[SourceFile, ...]

    def __post_init__(self) -> None:
        expected = f"{self.first_year % 100:02d}{self.last_year % 100:02d}"
        if self.code != expected or self.first_year > self.last_year:
            raise ValueError(
                f"Release code {self.code!r} does not match its survey years "
                f"{self.first_year}-{self.last_year} (expected code {expected!r})"
            )

    @property
    def years(self) -> tuple[int, ...]:
        return tuple(range(self.first_year, self.last_year + 1))

    @property
    def label(self) -> str:
        """Short year span used in user-facing text, e.g. "2003-25"."""
        return f"{self.first_year}-{self.last_year % 100:02d}"

    @property
    def lexicon_file(self) -> str:
        """Committed lexicon CSV (under data/reference/) for this release."""
        return f"activity_lexicon_{self.code}.csv"


def multiyear_release(
    code: str,
    *,
    first_year: int,
    last_year: int,
    expected_rows: dict[str, int],
    cps_column_count: int = 265,
    summary_column_count: int = 456,
) -> Release:
    """Describe a standard BLS multi-year release.

    BLS names every multi-year archive ``<stem>-<code>.zip`` with a
    ``<stem>_<code>.dat`` data member, so a new release needs only its code,
    its year span, and the official record counts from each ``*_info.txt``
    (``expected_rows``, keyed by source key). The column counts of the two
    very wide files are parameters because they can grow between releases.
    """
    label = f"{first_year}-{last_year % 100:02d}"

    def bls(name: str) -> str:
        return f"{_BLS_DATAFILES}/{name}"

    def archive(stem: str) -> dict[str, str]:
        return {
            "url": bls(f"{stem}-{code}.zip"),
            "zip_name": f"{stem}-{code}.zip",
            "dat_name": f"{stem}_{code}.dat",
        }

    files = (
        SourceFile(
            key="respondent",
            description=f"ATUS {label} Respondent file (one record per respondent)",
            **archive("atusresp"),
            expected_rows=expected_rows["respondent"],
            expected_columns=RESPONDENT_COLUMNS,
        ),
        SourceFile(
            key="roster",
            description=(
                f"ATUS {label} Roster file (household members and own nonhousehold "
                "children under 18)"
            ),
            **archive("atusrost"),
            expected_rows=expected_rows["roster"],
            expected_columns=ROSTER_COLUMNS,
        ),
        SourceFile(
            key="activity",
            description=(
                f"ATUS {label} Activity file (one record per diary-day activity episode)"
            ),
            **archive("atusact"),
            expected_rows=expected_rows["activity"],
            expected_columns=ACTIVITY_COLUMNS,
        ),
        SourceFile(
            key="who",
            description=(
                f"ATUS {label} Who file (one record per 'who was present' code per episode)"
            ),
            **archive("atuswho"),
            expected_rows=expected_rows["who"],
            expected_columns=WHO_COLUMNS,
        ),
        SourceFile(
            key="cps",
            description=(
                f"ATUS-CPS {label} file (CPS records for selected persons and their "
                "household members)"
            ),
            **archive("atuscps"),
            expected_rows=expected_rows["cps"],
            expected_column_count=cps_column_count,
            required_columns=CPS_REQUIRED_COLUMNS,
            notes=(
                "Only a documented subset of columns is loaded, and only for households "
                "of ATUS respondents. The full file (including nonrespondents) remains "
                "available in data/raw and data/staging."
            ),
        ),
        SourceFile(
            key="replicate_weights",
            description=f"ATUS {label} Replicate weights file (160 replicates of TUFNWGTP)",
            **archive("atuswgts"),
            expected_rows=expected_rows["replicate_weights"],
            expected_columns=REPLICATE_WEIGHT_COLUMNS,
        ),
        SourceFile(
            key="pandemic_weights",
            description="ATUS 2019-20 Pandemic replicate weights file (160 replicates of TU20FWGT)",
            url=bls("atuswgtspan-1920.zip"),
            zip_name="atuswgtspan-1920.zip",
            dat_name="atuswgtspan_1920.dat",
            expected_rows=expected_rows["pandemic_weights"],
            expected_columns=PANDEMIC_WEIGHT_COLUMNS,
            notes="Fixed 2019-2020 span; independent of the multi-year release suffix.",
        ),
        SourceFile(
            key="activity_summary",
            description=(
                f"ATUS {label} Activity summary file (BLS-derived per-respondent "
                "activity totals)"
            ),
            **archive("atussum"),
            expected_rows=expected_rows["activity_summary"],
            expected_column_count=summary_column_count,
            required_columns=("TUCASEID",),
            load=False,
            notes=(
                "Not loaded into the canonical database: its activity totals are derivable "
                "from the Activity file. It is staged and used as an independent "
                "cross-check of loaded episode durations (see validation)."
            ),
        ),
    )
    return Release(code=code, first_year=first_year, last_year=last_year, files=files)


# The release registry. Adding a release is one entry here (plus its
# committed lexicon CSV); see docs/source-data.md, "Adding the next release".
_REGISTRY: dict[str, Release] = {
    release.code: release
    for release in (
        multiyear_release(
            "0325",
            first_year=2003,
            last_year=2025,
            expected_rows={
                "respondent": 258_954,
                "roster": 702_409,
                "activity": 4_994_172,
                "who": 6_358_042,
                "cps": 1_583_133,
                "replicate_weights": 258_954,
                "pandemic_weights": 18_217,
                "activity_summary": 258_954,
            },
        ),
    )
}


def known_releases() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def get_release(release: str) -> Release:
    try:
        return _REGISTRY[release]
    except KeyError as exc:
        known = ", ".join(known_releases())
        raise KeyError(f"Unknown ATUS release {release!r}. Known releases: {known}") from exc


def register_release(release: Release) -> None:
    """Add a release to the registry at runtime (tests and tooling; committed
    releases are entries in ``_REGISTRY``). Refuses to replace an entry."""
    if release.code in _REGISTRY:
        raise ValueError(f"ATUS release {release.code!r} is already registered")
    _REGISTRY[release.code] = release


def unregister_release(code: str) -> None:
    """Remove a release added with :func:`register_release`."""
    _REGISTRY.pop(code, None)


def release_files(release: str) -> tuple[SourceFile, ...]:
    return get_release(release).files


def get_source(release: str, key: str) -> SourceFile:
    for source in release_files(release):
        if source.key == key:
            return source
    raise KeyError(f"No source {key!r} in release {release!r}")


def lexicon_path(reference_dir: Path, release: str) -> Path:
    """Path of the committed activity-lexicon CSV for a release."""
    return reference_dir / get_release(release).lexicon_file


def lexicon_label(release: str | None) -> str | None:
    """Year span of a release's harmonized lexicon, e.g. "2003-25".

    Used for text that names the loaded data's lexicon. A release code this
    code version does not know (a database loaded by a newer pipeline) still
    gets a label when it has the standard two-digit-years form; anything else
    (``None``, "unloaded") yields ``None`` and callers fall back to neutral
    wording.
    """
    if release is None:
        return None
    if release in _REGISTRY:
        return _REGISTRY[release].label
    if len(release) == 4 and release.isdigit() and release[:2] <= release[2:]:
        return f"20{release[:2]}-{release[2:]}"
    return None

