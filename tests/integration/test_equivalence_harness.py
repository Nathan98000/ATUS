"""scripts/equivalence end to end on the fixture databases.

Real Phase 5 API servers (subprocesses started by the harness exactly as for
a release check) answer from the hand-computed fixture loaded through the
real loader (target b) and from a serving database built from it by the
real builder (target c). The harness must find them equivalent, and must
find, with JSON paths, a serving copy whose data was changed: one episode's
minutes (a point estimate) and one replicate weight by one part in a
million (only standard errors and CI bounds move).

Target a (the Phase 1-4 checkout) is not run here: it needs a separate
checkout of the baseline commit (scripts/equivalence/run.sh setup). Its
server environment and command are unit-tested.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from atus_pipeline.analytics.weights import MULTIYEAR
from atus_pipeline.loading.loader import load_all
from atus_pipeline.serving.builder import build_serving_database
from tests.integration.analytics_dataset import E1, P9, SLEEP, analytics_settings
from tests.integration.conftest import _admin_url, create_scratch_database, drop_scratch_database

pytestmark = pytest.mark.integration

HARNESS = Path(__file__).resolve().parents[2] / "scripts" / "equivalence"
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))  # replay.py imports its siblings by name

import replay  # noqa: E402

SPECS = [
    {"id": "sleep-2023", "endpoint": "estimate", "category": "estimate", "expect": "ok",
     "body": {"activity": {"include": [SLEEP]}, "years": [2023]}},
    {"id": "sleep-2019", "endpoint": "estimate", "category": "estimate", "expect": "ok",
     "body": {"activity": {"include": [SLEEP]}, "years": [2019]}},
    {"id": "participation-pooled", "endpoint": "estimate", "category": "estimate",
     "expect": "ok", "body": {"measure": "participation_rate",
                              "activity": {"include": ["01"]}, "years": [2019, 2023]}},
    {"id": "trend-across-2020", "endpoint": "trend", "category": "trend", "expect": "ok",
     "body": {"activity": {"preset": "sleep"}, "years": [2019, 2020, 2023]}},
    {"id": "pandemic", "endpoint": "estimate", "category": "pandemic", "expect": "ok",
     "body": {"activity": {"include": [SLEEP]}, "years": [2019, 2020],
              "weights": "pandemic"}},
    {"id": "men-vs-women", "endpoint": "compare", "category": "compare", "expect": "ok",
     "body": {"activity": {"include": [SLEEP]}, "years": [2023], "group_a": {"sex": "male"},
              "group_b": {"sex": "female"}}},
    {"id": "2020-multiyear", "endpoint": "estimate", "category": "error", "expect": "error",
     "body": {"activity": {"include": [SLEEP]}, "years": [2020]}},
    {"id": "unknown-code", "endpoint": "estimate", "category": "error", "expect": "error",
     "body": {"activity": {"include": ["010198"]}, "years": [2023]}},
    {"id": "schema", "endpoint": "trend", "category": "error", "expect": "error",
     "body": {"activity": {"include": [SLEEP]}, "years": [2023], "colour": "blue"}},
    {"id": "text-plain", "endpoint": "estimate", "category": "error", "expect": "error",
     "content_type": "text/plain", "body": {"activity": {"include": [SLEEP]},
                                            "years": [2023]}},
]


def _url(test_db_url: str, name: str) -> str:
    return urlunsplit(urlsplit(test_db_url)._replace(path=f"/{name}"))


def _new_database(test_db_url: str, suffix: str, template: str | None = None) -> str:
    name = f"{urlsplit(test_db_url).path.lstrip('/')}_{suffix}"
    with psycopg.connect(_admin_url(test_db_url), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        source = f' TEMPLATE "{urlsplit(template).path.lstrip("/")}"' if template else ""
        conn.execute(f'CREATE DATABASE "{name}"{source}')
    return _url(test_db_url, name)


@pytest.fixture(scope="module")
def databases(test_db_url, tmp_path_factory):
    full = create_scratch_database(test_db_url, "eqh_full")
    settings = analytics_settings(tmp_path_factory.mktemp("eqh"), full)
    with psycopg.connect(full) as conn:
        load_all(conn, settings)
    serving = _new_database(test_db_url, "eqh_serving")
    build_serving_database(full, serving, require_validated=False)
    yield full, serving
    for url in (full, serving):
        drop_scratch_database(url)


@pytest.fixture
def changed_serving(databases, test_db_url):
    """A copy of the serving database with one episode's minutes and one
    replicate weight changed."""
    _, serving = databases
    url = _new_database(test_db_url, "eqh_changed", template=serving)
    column = MULTIYEAR.replicate_columns[0]
    with psycopg.connect(url) as conn:
        changed = conn.execute(
            "UPDATE atus.activities SET duration_minutes = duration_minutes + 1 "
            "WHERE tucaseid = %s AND activity_code = %s", (int(P9), SLEEP)).rowcount
        assert changed >= 1
        changed = conn.execute(
            f"UPDATE atus.replicate_weights SET {column} = {column} * (1 + 1e-6) "
            "WHERE tucaseid = %s", (int(E1),)).rowcount
        assert changed == 1
    yield url
    drop_scratch_database(url)


def _ports() -> str:
    """Three loopback ports: ``$ATUS_EQ_TEST_PORTS`` (e.g. ``8644,8645,8646``,
    for a machine with reserved ranges), else three free ephemeral ones."""
    if os.environ.get("ATUS_EQ_TEST_PORTS"):
        return os.environ["ATUS_EQ_TEST_PORTS"]
    sockets = []
    try:
        for _ in range(3):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return ",".join(str(s.getsockname()[1]) for s in sockets)
    finally:
        for sock in sockets:
            sock.close()


def _run(tmp_path, monkeypatch, full: str, serving: str, *extra: str) -> tuple[int, dict]:
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "fixture.jsonl").write_text("\n".join(json.dumps(s) for s in SPECS) + "\n")
    monkeypatch.setenv(replay.FULL_URL_ENV, full)
    monkeypatch.setenv(replay.SERVING_URL_ENV, serving)
    out = tmp_path / "out"
    code = replay.main(["--specs", str(specs), "--out", str(out), "--targets", "bc",
                        "--ports", _ports(), "--activity-sample", "6", *extra])
    report = json.loads((out / "report.json").read_text()) if (out / "report.json").exists() \
        else {}
    return code, report


def test_full_and_serving_fixture_databases_are_equivalent_over_http(
        databases, tmp_path, monkeypatch):
    full, serving = databases
    code, report = _run(tmp_path, monkeypatch, full, serving)
    summary = report["summary"]
    failed = [s for s in report["specs"] if not s["passed"]]
    assert code == 0, (failed, summary["gets_failed"])
    assert summary["passed"] and summary["specs"] == len(SPECS)
    assert summary["expectation_misses"] == []
    assert set(report["servers"]) == {"b", "c"}
    b, c = report["servers"]["b"], report["servers"]["c"]
    assert ":serving-" in c["data_version"] and c["data_version"] != b["data_version"]
    assert b["served_data_version"] == b["data_version"]
    assert c["served_data_version"] == c["data_version"]
    assert b["read_only"] == "on" and c["read_only"] == "on"
    assert "ATUS_DATABASE_URL" not in b["settings"]
    assert summary["pairs"]["b_c"]["outcomes"] == {"equivalent": len(SPECS) + summary["gets"]}
    by_id = {s["id"]: s for s in report["specs"]}
    assert by_id["sleep-2023"]["targets"] == {"b": {"status": 200}, "c": {"status": 200}}
    assert by_id["text-plain"]["targets"]["c"] == {"status": 415,
                                                   "code": "unsupported_media_type"}
    paths = [g["path"] for g in report["gets"]]
    assert "/api/v1/meta" in paths and "/api/v1/activities/01" in paths
    summary_md = (tmp_path / "out" / "summary.md").read_text()
    assert summary_md.startswith("# HTTP equivalence report\n\n**PASS**")
    for log in (tmp_path / "out" / "logs").iterdir():
        assert "postgresql://" not in log.read_text() or ":***@" in log.read_text()


# CREATE DATABASE ... TEMPLATE refuses a source that another session is connected to,
# and a transaction pooler keeps its server connections open.
@pytest.mark.direct_connection
def test_a_changed_serving_database_is_caught_with_json_paths(
        databases, changed_serving, tmp_path, monkeypatch):
    full, _ = databases
    code, report = _run(tmp_path, monkeypatch, full, changed_serving, "--no-gets")
    assert code == 1
    by_id = {s["id"]: s for s in report["specs"]}

    def paths(spec_id):
        return {m["path"] for m in by_id[spec_id]["pairs"]["b_c"].get("mismatches", [])}

    # One more minute of sleep for a 2019 respondent: the point estimate moves.
    assert by_id["sleep-2019"]["status"] == "mismatch"
    assert "$.estimate.value" in paths("sleep-2019")
    # A replicate weight off by 1e-6: points stay exact, the SE and CI do not.
    assert by_id["sleep-2023"]["status"] == "mismatch"
    assert paths("sleep-2023") <= {"$.estimate.standard_error", "$.estimate.ci_lower",
                                   "$.estimate.ci_upper"}
    assert "$.estimate.standard_error" in paths("sleep-2023")
    # Refusals do not depend on the data and stay identical.
    assert by_id["2020-multiyear"]["status"] == "equivalent"
    saved = list((tmp_path / "out" / "responses" / "specs").glob("sleep-2019-*.c.json"))
    assert len(saved) == 1
    assert "Failed specs" in (tmp_path / "out" / "summary.md").read_text()
