"""`atus build-serving-db` (brief §2, §9; ADR-013): reproducible, equivalent
to the full database, shaped as designed, guarded, and all-or-nothing.

The full databases are the randomized fixture (``random_dataset.py``: zero
weights, all-NULL 2020 replicate rows, all-zero pandemic rows, rosters and
CPS households beyond line 1, repeated codes and 0-minute episodes) and the
hand-computed fixture, both loaded through the real loader.
"""

from __future__ import annotations

import json
import random
import re
import secrets
from dataclasses import replace
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from atus_pipeline.analytics import AnalysisEngine
from atus_pipeline.api.app import create_app
from atus_pipeline.cli import cli
from atus_pipeline.config import Settings
from atus_pipeline.database.versions import read_data_version
from atus_pipeline.loading.loader import load_all
from atus_pipeline.serving import builder
from atus_pipeline.serving.builder import ServingBuildError, build_serving_database
from atus_pipeline.serving.content import ContentHash, compute_content_hash
from atus_pipeline.serving.contract import READ, missing_contract_columns
from atus_pipeline.serving.equivalence import compare_results, random_specs, run, seed_specs
from atus_pipeline.serving.guard import SERVING_MARKER, serving_marker
from atus_pipeline.serving.shape import RESHAPED, missing_checks, read_shapes
from atus_pipeline.validation.results import CheckResult, persist_results
from tests.integration.analytics_dataset import analytics_settings
from tests.integration.conftest import (
    _admin_url,
    create_scratch_database,
    drop_scratch_database,
)
from tests.integration.random_dataset import random_settings

pytestmark = pytest.mark.integration

VERSION = re.compile(r"^0325:serving-\d{8}T\d{6}Z-[0-9a-f]{8}:[0-9a-f]{12}$")
REPLICATES = READ["replicate_weights"][1:]


def _url(test_db_url: str, name: str) -> str:
    return urlunsplit(urlsplit(test_db_url)._replace(path=f"/{name}"))


def empty_database(test_db_url: str, suffix: str) -> str:
    """A new database with nothing in it (not even the migrations)."""
    name = f"{urlsplit(test_db_url).path.lstrip('/')}_{suffix}"
    with psycopg.connect(_admin_url(test_db_url), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')
    return _url(test_db_url, name)


def clone_database(test_db_url: str, template_url: str, suffix: str) -> str:
    name = f"{urlsplit(test_db_url).path.lstrip('/')}_{suffix}"
    template = urlsplit(template_url).path.lstrip("/")
    with psycopg.connect(_admin_url(test_db_url), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
    return _url(test_db_url, name)


@pytest.fixture(scope="module")
def full(test_db_url, tmp_path_factory):
    url = create_scratch_database(test_db_url, "srvb_full")
    settings = random_settings(tmp_path_factory.mktemp("srvb"), url)
    with psycopg.connect(url) as conn:
        load_all(conn, settings)
    yield url
    drop_scratch_database(url)


@pytest.fixture(scope="module")
def builds(full, test_db_url):
    """Two builds from the same full database: one into an empty database,
    one into a freshly migrated one."""
    first = empty_database(test_db_url, "srvb_a")
    second = create_scratch_database(test_db_url, "srvb_b")
    reports = [build_serving_database(full, url, require_validated=False)
               for url in (first, second)]
    yield (first, reports[0]), (second, reports[1])
    for url in (first, second):
        drop_scratch_database(url)


@pytest.fixture
def mutable_full(full, test_db_url):
    """A disposable copy of the full database, for tests that alter the source."""
    url = clone_database(test_db_url, full, "srvb_mut")
    yield url
    drop_scratch_database(url)


@pytest.fixture
def target(test_db_url):
    url = create_scratch_database(test_db_url, "srvb_target")
    yield url
    drop_scratch_database(url)


# --------------------------------------------------------------------------- #
# reproducible, and a new data version per build                               #
# --------------------------------------------------------------------------- #

def test_two_builds_have_the_same_content_hash_and_different_versions(builds):
    (first, a), (second, b) = builds
    assert a.content_hash == b.content_hash
    assert a.tables["activities"]["sha256"] == b.tables["activities"]["sha256"]
    assert a.build_id != b.build_id and a.data_version != b.data_version
    for url, report in builds:
        assert VERSION.match(report.data_version)
        assert report.data_version.endswith(":" + report.content_hash[:12])
        with psycopg.connect(url) as conn:
            assert read_data_version(conn) == report.data_version
            assert compute_content_hash(conn).content_hash == report.content_hash


def test_the_content_hash_changes_with_the_data(mutable_full, target, builds):
    (_, reference), _ = builds
    with psycopg.connect(mutable_full) as conn:
        conn.execute(
            "UPDATE atus.activities SET duration_minutes = duration_minutes + 1 "
            "WHERE (tucaseid, activity_number) = (SELECT tucaseid, max(activity_number) "
            "FROM atus.activities GROUP BY tucaseid ORDER BY tucaseid LIMIT 1)"
        )
    report = build_serving_database(mutable_full, target, require_validated=False)
    assert report.content_hash != reference.content_hash
    assert report.tables["activities"]["sha256"] != reference.tables["activities"]["sha256"]
    assert report.tables["respondents"]["sha256"] == reference.tables["respondents"]["sha256"]


# --------------------------------------------------------------------------- #
# equivalent: the engine and the API serve the same results                   #
# --------------------------------------------------------------------------- #

def _specs(conn, n, seed):
    years = [y for (y,) in conn.execute(
        "SELECT DISTINCT data_year FROM atus.respondents ORDER BY 1")]
    tier1 = [c for (c,) in conn.execute("SELECT code FROM atus.activity_tier1 ORDER BY 1")]
    leaves = [c for (c,) in conn.execute(
        "SELECT DISTINCT activity_code FROM atus.activities ORDER BY 1")]
    return seed_specs(years, tier1) + random_specs(random.Random(seed), years=years,
                                                   tier1=tier1, leaves=leaves, n=n)


def _assert_equivalent(full_url: str, serving_url: str, n: int, seed: int, minimum: int):
    with psycopg.connect(full_url) as f, psycopg.connect(serving_url) as s:
        specs = _specs(f, n, seed)
        engines = AnalysisEngine(f), AnalysisEngine(s)
        results = [[run(engine, op, spec) for engine in engines] for op, spec in specs]
    for (op, spec), (a, b) in zip(specs, results, strict=True):
        comparison = compare_results(a, b)
        assert comparison.equivalent, (op, spec, comparison.mismatches[:3])
    assert sum(isinstance(a, dict) for a, _ in results) >= minimum


def test_the_serving_database_serves_the_same_results(full, builds):
    (serving, _), _ = builds
    _assert_equivalent(full, serving, n=250, seed=21, minimum=150)


def test_the_hand_computed_fixture_serves_the_same_results(test_db_url, tmp_path):
    full_url = create_scratch_database(test_db_url, "srvb_hand_full")
    serving_url = create_scratch_database(test_db_url, "srvb_hand_srv")
    try:
        with psycopg.connect(full_url) as conn:
            load_all(conn, analytics_settings(tmp_path, full_url))
        report = build_serving_database(full_url, serving_url, require_validated=False)
        # E3's 0-minute sleep episode survives as a 0-minute (not participating) row
        with psycopg.connect(serving_url) as conn:
            assert conn.execute(
                "SELECT duration_minutes FROM atus.activities "
                "WHERE tucaseid = 20230101000003 AND activity_code = '010101'"
            ).fetchone() == (0,)
        assert report.counts["replicate_weights.omitted"] == 1        # P0, 2020
        assert report.counts["pandemic_replicate_weights.omitted"] == 1  # P8, gap window
        _assert_equivalent(full_url, serving_url, n=150, seed=22, minimum=40)
    finally:
        drop_scratch_database(full_url)
        drop_scratch_database(serving_url)


def _strip_versions(payload):
    if isinstance(payload, dict):
        return {k: _strip_versions(v) for k, v in payload.items() if "data_version" not in k}
    return payload


def test_the_api_serves_the_same_responses(full, builds, tmp_path):
    (serving, _), _ = builds
    analysis = {"activity": {"include": ["010101"]}, "years": [2023]}
    requests = [
        ("get", "/api/v1/meta", None), ("get", "/api/v1/activities", None),
        ("get", "/api/v1/activities/presets", None), ("get", "/api/v1/activities/12", None),
        ("get", "/api/v1/activities/010101", None), ("get", "/api/v1/population/metadata", None),
        ("post", "/api/v1/analysis/estimate", analysis),
        ("post", "/api/v1/analysis/estimate",
         {**analysis, "activity": {"preset": "leisure_and_sports_bls_table"},
          "years": [2018, 2019, 2021, 2023], "population": {"sex": "female"}}),
        ("post", "/api/v1/analysis/trend", {**analysis, "years": [2018, 2019, 2021, 2023]}),
        ("post", "/api/v1/analysis/compare",
         {**analysis, "group_a": {"sex": "male"}, "group_b": {"sex": "female"}}),
        ("post", "/api/v1/analysis/estimate", {**analysis, "weights": "pandemic",
                                               "years": [2019, 2020]}),
        ("post", "/api/v1/analysis/estimate", {**analysis, "years": [2002]}),
    ]
    bodies = []
    for url in (full, serving):
        settings = Settings(database_url=url, data_dir=tmp_path, release="0325",
                            log_level="WARNING")
        with TestClient(create_app(settings)) as client:
            bodies.append([
                (r.status_code, r.json()) for r in (
                    client.get(path) if method == "get" else client.post(path, json=body)
                    for method, path, body in requests
                )
            ])
    for (method, path, _), (status_a, a), (status_b, b) in zip(requests, *bodies, strict=True):
        assert status_a == status_b, path
        if method == "get":
            assert _strip_versions(a) == _strip_versions(b), path
        else:
            result = compare_results(a, b)
            assert result.equivalent, (path, result.mismatches[:3])
    assert [status for status, _ in bodies[0]].count(200) == len(requests) - 1


# --------------------------------------------------------------------------- #
# the shape (I01-I03) and the serving marker (F57)                             #
# --------------------------------------------------------------------------- #

def test_the_serving_schema_keeps_the_contract_and_every_constraint(full, builds):
    (serving, _), _ = builds
    with psycopg.connect(full) as f, psycopg.connect(serving) as s:
        canonical, shapes = read_shapes(f), read_shapes(s)
        assert missing_contract_columns(s) == []
        assert missing_checks(canonical, shapes) == []
        assert "activity_companions" not in shapes
        for table, reshape in RESHAPED.items():
            columns = {c.name: c for c in shapes[table].columns}
            assert tuple(columns) == READ[table]
            for name, column in columns.items():
                original = next(c for c in canonical[table].columns if c.name == name)
                assert column.not_null == original.not_null, (table, name)
                expected = reshape.type_overrides.get(name, original.type)
                assert column.type == expected, (table, name)
        constraints = {c.name for shape in shapes.values() for c in shape.constraints}
        assert {"respondents_pkey", "household_members_pkey", "cps_persons_pkey",
                "replicate_weights_pkey", "pandemic_replicate_weights_pkey",
                "final_weight_2020_rule", "pandemic_weight_years", "serving_line1_only",
                "activities_tucaseid_fkey", "activities_activity_code_fkey",
                "activities_duration_minutes_check"} <= constraints
        assert "activities_pkey" not in constraints
        assert {i.name for i in shapes["activities"].indexes} == {"idx_activities_activity_code"}
        assert {i.name for i in shapes["respondents"].indexes} == {
            "idx_respondents_data_year", "idx_respondents_diary_date"}
        # I01: summed minutes stay smallint, so sum() still returns bigint
        assert s.execute(
            "SELECT pg_typeof(sum(duration_minutes))::text FROM atus.activities"
        ).fetchone() == ("bigint",)


def test_the_served_rows_are_the_designed_subset(full, builds):
    (serving, report), _ = builds
    with psycopg.connect(full) as f, psycopg.connect(serving) as s:
        def one(conn, sql):
            return conn.execute(sql).fetchone()[0]

        # activities: one row per (respondent, code) with the summed minutes
        assert one(s, "SELECT count(*) FROM atus.activities") == one(
            f, "SELECT count(*) FROM (SELECT DISTINCT tucaseid, activity_code "
               "FROM atus.activities) g")
        totals = "SELECT tucaseid, sum(duration_minutes) FROM atus.activities GROUP BY 1 ORDER BY 1"
        assert f.execute(totals).fetchall() == s.execute(totals).fetchall()
        assert one(s, "SELECT count(*) FROM (SELECT activity_code, lag(activity_code) OVER "
                      "(ORDER BY ctid) AS previous FROM atus.activities) o "
                      "WHERE previous > activity_code") == 0
        # line-1 roster and CPS rows only
        for table in ("household_members", "cps_persons"):
            assert one(s, f"SELECT count(*) FROM atus.{table} WHERE lineno <> 1") == 0
            assert one(s, f"SELECT count(*) FROM atus.{table}") == one(
                f, f"SELECT count(*) FROM atus.{table} WHERE lineno = 1")
        # omitted replicate rows: exactly the 2020 rows and the gap-window rows
        all_null = " AND ".join(f"{c} IS NULL" for c in REPLICATES)
        assert report.counts["replicate_weights.omitted"] == one(
            f, f"SELECT count(*) FROM atus.replicate_weights WHERE {all_null}") == 60
        assert one(s, "SELECT count(*) FROM atus.replicate_weights") == one(
            f, "SELECT count(*) FROM atus.respondents WHERE final_weight IS NOT NULL")
        assert report.counts["pandemic_replicate_weights.omitted"] == one(
            f, "SELECT count(*) FROM atus.respondents WHERE pandemic_weight = 0") > 0
        # respondents and their NULLs are all kept
        assert one(s, "SELECT count(*) FROM atus.respondents") == one(
            f, "SELECT count(*) FROM atus.respondents")
        assert one(s, "SELECT count(*) FROM atus.cps_persons WHERE education IS NULL") > 0
        assert one(s, "SELECT count(*) FROM atus.validation_results") == 0


def test_one_succeeded_run_describes_the_source_data(full, builds):
    (serving, report), _ = builds
    with psycopg.connect(full) as f, psycopg.connect(serving) as s:
        source = f.execute(
            "SELECT id, release, pipeline_version, started_at, finished_at "
            "FROM atus.ingestion_runs WHERE status = 'succeeded' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        runs = s.execute(
            "SELECT id, release, pipeline_version, started_at, finished_at, status, notes, "
            "data_version FROM atus.ingestion_runs"
        ).fetchall()
        assert len(runs) == 1 and runs[0][:5] == source and runs[0][5] == "succeeded"
        notes = json.loads(runs[0][6])
        assert notes["kind"] == "serving" and notes["build_id"] == report.build_id
        assert notes["content_hash"] == report.content_hash
        assert notes["source_data_version"] == read_data_version(f)
        assert runs[0][7] == report.data_version
        assert s.execute("SELECT count(*) FROM atus.source_files").fetchone()[0] == f.execute(
            "SELECT count(*) FROM atus.source_files WHERE ingestion_run_id = %s", (source[0],)
        ).fetchone()[0] > 0
        comment = serving_marker(s)
        assert comment and comment.startswith(f"schema comment '{SERVING_MARKER} build ")


@pytest.mark.parametrize("command", [["migrate"], ["load"], ["validate-db"]])
def test_full_database_commands_refuse_the_serving_database(builds, tmp_path, command):
    (serving, report), _ = builds
    result = CliRunner().invoke(cli, command, env={
        "ATUS_DATABASE_URL": serving, "ATUS_DATA_DIR": str(tmp_path),
        "ATUS_LOG_LEVEL": "WARNING",
    })
    assert result.exit_code != 0
    assert "serving database" in result.output
    with psycopg.connect(serving) as conn:
        assert read_data_version(conn) == report.data_version


# --------------------------------------------------------------------------- #
# the source is only read; the target is all-or-nothing                        #
# --------------------------------------------------------------------------- #

def test_a_select_only_role_can_build_from_the_source(full, target, test_db_url):
    role = f"{urlsplit(test_db_url).path.lstrip('/')}_srvb_reader"
    password = secrets.token_urlsafe(16)
    dbname = urlsplit(full).path.lstrip("/")
    with psycopg.connect(full, autocommit=True) as conn:
        conn.execute(f'DROP ROLE IF EXISTS "{role}"')
        conn.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}'")
        conn.execute(f'GRANT USAGE ON SCHEMA atus TO "{role}"')
        conn.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA atus TO "{role}"')
        before = conn.execute(
            "SELECT (SELECT count(*) FROM atus.ingestion_runs), "
            "(SELECT count(*) FROM atus.validation_results)").fetchone()
    parts = urlsplit(full)
    reader = urlunsplit(parts._replace(netloc=f"{role}:{password}@{parts.hostname}:{parts.port}"))
    try:
        report = build_serving_database(reader, target, require_validated=False)
        assert report.data_version
        with psycopg.connect(full) as conn:
            assert conn.execute(
                "SELECT (SELECT count(*) FROM atus.ingestion_runs), "
                "(SELECT count(*) FROM atus.validation_results)").fetchone() == before
    finally:
        with psycopg.connect(full, autocommit=True) as conn:
            conn.execute(f'REVOKE ALL ON ALL TABLES IN SCHEMA atus FROM "{role}"')
            conn.execute(f'REVOKE ALL ON SCHEMA atus FROM "{role}"')
        with psycopg.connect(_admin_url(test_db_url), autocommit=True) as conn:
            conn.execute(f'REVOKE ALL ON DATABASE "{dbname}" FROM "{role}"')
            conn.execute(f'DROP ROLE IF EXISTS "{role}"')


def _untouched(url: str) -> None:
    """The target still holds only the freshly migrated, empty canonical schema."""
    with psycopg.connect(url) as conn:
        assert serving_marker(conn) is None
        assert conn.execute("SELECT count(*) FROM atus.ingestion_runs").fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'atus' "
            "AND table_name = 'activities' AND column_name = 'activity_number'"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT to_regclass('atus.activity_companions')").fetchone()[0]


def test_an_unconvertible_replicate_weight_stops_the_build(mutable_full, target):
    with psycopg.connect(mutable_full) as conn:
        conn.execute(
            "UPDATE atus.replicate_weights SET tufnwgtp007 = 123456789012.123457 "
            "WHERE tucaseid = (SELECT min(tucaseid) FROM atus.respondents "
            "WHERE final_weight > 0)"
        )
    with pytest.raises(ServingBuildError, match="numeric -> float8 -> numeric round trip"):
        build_serving_database(mutable_full, target, require_validated=False)
    _untouched(target)


def test_an_omitted_row_that_an_estimate_would_read_stops_the_build(mutable_full, target):
    with psycopg.connect(mutable_full) as conn:
        conn.execute(
            "UPDATE atus.replicate_weights SET "
            + ", ".join(f"{c} = NULL" for c in REPLICATES)
            + " WHERE tucaseid = (SELECT min(tucaseid) FROM atus.respondents "
              "WHERE final_weight > 0)"
        )
    with pytest.raises(ServingBuildError, match="final_weight > 0"):
        build_serving_database(mutable_full, target, require_validated=False)
    _untouched(target)


def test_a_failure_after_the_copy_leaves_the_target_unchanged(full, target, monkeypatch):
    real = builder.compute_content_hash

    def corrupted(conn, schema="atus"):
        result = real(conn, schema)
        tables = list(result.tables)
        tables[0] = replace(tables[0], sha256="0" * 64)
        return ContentHash(content_hash="0" * 64, tables=tuple(tables))

    monkeypatch.setattr(builder, "compute_content_hash", corrupted)
    with pytest.raises(ServingBuildError, match="content hash mismatch.*activity_tier1"):
        build_serving_database(full, target, require_validated=False)
    _untouched(target)


def test_the_build_refuses_a_target_that_is_not_new(full, builds, target):
    (serving, _), _ = builds
    with pytest.raises(ServingBuildError, match="already a serving database"):
        build_serving_database(full, serving, require_validated=False)
    with psycopg.connect(target) as conn:
        conn.execute("INSERT INTO atus.activity_tier1 VALUES ('01', 'x')")
    with pytest.raises(ServingBuildError, match="not empty"):
        build_serving_database(full, target, require_validated=False)
    with pytest.raises(ServingBuildError, match="same database"):
        build_serving_database(full, full.replace("localhost", "127.0.0.1"),
                               require_validated=False)


def test_the_build_refuses_a_serving_database_as_its_source(builds, target):
    (serving, _), _ = builds
    with pytest.raises(ServingBuildError, match="source is a serving database"):
        build_serving_database(serving, target, require_validated=False)
    _untouched(target)


def test_the_build_requires_recorded_validation_by_default(mutable_full, target):
    with pytest.raises(ServingBuildError, match="validate-db.*validate-analytics"):
        build_serving_database(mutable_full, target)
    with psycopg.connect(mutable_full) as conn:
        persist_results(conn, [CheckResult("row counts", True, "error", "ok")])
        conn.commit()
        persist_results(conn, [CheckResult("analytics: A1-2025: sleeping", False, "error",
                                           "8.9", "expected 9.0")])
        conn.commit()
    with pytest.raises(ServingBuildError, match="1 failed check"):
        build_serving_database(mutable_full, target)
    with psycopg.connect(mutable_full) as conn:
        persist_results(conn, [CheckResult("analytics: A1-2025: sleeping", True, "error",
                                           "9.0")])
        conn.commit()
    report = build_serving_database(mutable_full, target)
    assert report.validation["validate_db"]["checks"] == 1
    assert report.validation["validate_analytics"]["failed_errors"] == 0


# --------------------------------------------------------------------------- #
# the commands                                                                 #
# --------------------------------------------------------------------------- #

def _cli(*args: str):
    return CliRunner().invoke(cli, list(args), env={"ATUS_LOG_LEVEL": "WARNING"})


def test_the_commands_build_verify_and_compare(full, target, tmp_path):
    report_path = tmp_path / "report.json"
    built = _cli("build-serving-db", "--source", full, "--target", target,
                 "--allow-unvalidated", "--report", str(report_path))
    assert built.exit_code == 0, built.output
    report = json.loads(report_path.read_text())
    assert f"Data version:  {report['data_version']}" in built.output
    assert "Omitted rows (excluded by every estimate): replicate_weights 60" in built.output
    assert "postgresql://" not in built.output

    verified = _cli("verify-serving-db", "--database-url", target, "--content-hash", "--explain")
    assert verified.exit_code == 0, verified.output
    assert "[PASS] serving: content hash" in verified.output
    assert "[PASS] serving: plan: sleep (010101) pooled" in verified.output

    compared = _cli("compare-serving-db", "--full", full, "--serving", target, "--specs", "25",
                    "--report", str(tmp_path / "diff.json"))
    assert compared.exit_code == 0, compared.output
    assert " equivalent, 0 not" in compared.output
    diff = json.loads((tmp_path / "diff.json").read_text())
    assert diff["specs"] >= 25 and all(r["equivalent"] for r in diff["results"])


def test_verify_reports_a_database_that_does_not_match_its_manifest(builds, tmp_path):
    (serving, report), _ = builds
    manifest = {"kind": "atus-serving-db", "data_version": report.data_version,
                "content_hash": report.content_hash, "release": "0325",
                "build_id": report.build_id, "artifact": {},
                "tables": {t: {"rows": v["rows"] + (t == "activities")}
                           for t, v in report.tables.items() if "rows" in v}}
    path = tmp_path / "m.manifest.json"
    path.write_text(json.dumps(manifest))
    result = _cli("verify-serving-db", "--database-url", serving, "--manifest", str(path))
    assert result.exit_code == 1
    assert "[FAIL] serving: rows in activities" in result.output


def test_the_build_command_explains_a_refusal_without_a_traceback(full, target):
    result = _cli("build-serving-db", "--source", full, "--target", target)
    assert result.exit_code != 0
    assert "the source database is not validated" in result.output
    assert "Traceback" not in result.output
