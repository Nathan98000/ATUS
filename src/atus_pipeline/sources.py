"""Declarative registry of official BLS ATUS source files.

Each multi-year "release" (e.g. ``0325`` = survey years 2003-2025) is described
by a tuple of :class:`SourceFile` entries. Everything the pipeline knows about
a source file lives here: its official URL, the exact archive member holding
the data, the expected header, and the official record count published by BLS
in the ``*_info.txt`` file inside each archive.

Adding a future release (e.g. ``0326``) means adding one ``_build_release``
call with the new row counts and, if BLS changes any file layout, updating the
header constants — nothing else in the pipeline should need to change.

Sources:
    https://www.bls.gov/tus/data/datafiles-0325.htm
"""

from __future__ import annotations

from dataclasses import dataclass, field

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


def _build_release_0325() -> tuple[SourceFile, ...]:
    def bls(name: str) -> str:
        return f"{_BLS_DATAFILES}/{name}"

    return (
        SourceFile(
            key="respondent",
            description="ATUS 2003-25 Respondent file (one record per respondent)",
            url=bls("atusresp-0325.zip"),
            zip_name="atusresp-0325.zip",
            dat_name="atusresp_0325.dat",
            expected_rows=258_954,
            expected_columns=RESPONDENT_COLUMNS,
        ),
        SourceFile(
            key="roster",
            description=(
                "ATUS 2003-25 Roster file (household members and own nonhousehold "
                "children under 18)"
            ),
            url=bls("atusrost-0325.zip"),
            zip_name="atusrost-0325.zip",
            dat_name="atusrost_0325.dat",
            expected_rows=702_409,
            expected_columns=ROSTER_COLUMNS,
        ),
        SourceFile(
            key="activity",
            description="ATUS 2003-25 Activity file (one record per diary-day activity episode)",
            url=bls("atusact-0325.zip"),
            zip_name="atusact-0325.zip",
            dat_name="atusact_0325.dat",
            expected_rows=4_994_172,
            expected_columns=ACTIVITY_COLUMNS,
        ),
        SourceFile(
            key="who",
            description="ATUS 2003-25 Who file (one record per 'who was present' code per episode)",
            url=bls("atuswho-0325.zip"),
            zip_name="atuswho-0325.zip",
            dat_name="atuswho_0325.dat",
            expected_rows=6_358_042,
            expected_columns=WHO_COLUMNS,
        ),
        SourceFile(
            key="cps",
            description=(
                "ATUS-CPS 2003-25 file (CPS records for selected persons and their "
                "household members)"
            ),
            url=bls("atuscps-0325.zip"),
            zip_name="atuscps-0325.zip",
            dat_name="atuscps_0325.dat",
            expected_rows=1_583_133,
            expected_column_count=265,
            required_columns=CPS_REQUIRED_COLUMNS,
            notes=(
                "Only a documented subset of columns is loaded, and only for households "
                "of ATUS respondents. The full file (including nonrespondents) remains "
                "available in data/raw and data/staging."
            ),
        ),
        SourceFile(
            key="replicate_weights",
            description="ATUS 2003-25 Replicate weights file (160 replicates of TUFNWGTP)",
            url=bls("atuswgts-0325.zip"),
            zip_name="atuswgts-0325.zip",
            dat_name="atuswgts_0325.dat",
            expected_rows=258_954,
            expected_columns=REPLICATE_WEIGHT_COLUMNS,
        ),
        SourceFile(
            key="pandemic_weights",
            description="ATUS 2019-20 Pandemic replicate weights file (160 replicates of TU20FWGT)",
            url=bls("atuswgtspan-1920.zip"),
            zip_name="atuswgtspan-1920.zip",
            dat_name="atuswgtspan_1920.dat",
            expected_rows=18_217,
            expected_columns=PANDEMIC_WEIGHT_COLUMNS,
            notes="Fixed 2019-2020 span; independent of the multi-year release suffix.",
        ),
        SourceFile(
            key="activity_summary",
            description=(
                "ATUS 2003-25 Activity summary file (BLS-derived per-respondent "
                "activity totals)"
            ),
            url=bls("atussum-0325.zip"),
            zip_name="atussum-0325.zip",
            dat_name="atussum_0325.dat",
            expected_rows=258_954,
            expected_column_count=456,
            required_columns=("TUCASEID",),
            load=False,
            notes=(
                "Not loaded into the canonical database: its activity totals are derivable "
                "from the Activity file. It is staged and used as an independent "
                "cross-check of loaded episode durations (see validation)."
            ),
        ),
    )


_REGISTRY: dict[str, tuple[SourceFile, ...]] = {
    "0325": _build_release_0325(),
}


def release_files(release: str) -> tuple[SourceFile, ...]:
    try:
        return _REGISTRY[release]
    except KeyError as exc:
        known = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"Unknown ATUS release {release!r}. Known releases: {known}") from exc


def get_source(release: str, key: str) -> SourceFile:
    for source in release_files(release):
        if source.key == key:
            return source
    raise KeyError(f"No source {key!r} in release {release!r}")
