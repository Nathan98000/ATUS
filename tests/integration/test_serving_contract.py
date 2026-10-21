"""The serving-DB contract is complete (audit fact I03).

A role is granted SELECT on exactly the contract's columns (and INSERT on
``validation_results``'s), nothing else. The engine over every population
dimension, weight scheme and variance mode, every HTTP endpoint, and the
benchmark suite then run as that role: any read outside the contract would
fail with "permission denied". Because a serving database keeps precisely
these columns, this proves the engine code runs unchanged on it. The same
role also shows the API path never writes.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import quote, urlsplit

import psycopg
import pytest
from fastapi.testclient import TestClient

from atus_pipeline.analytics import (
    ActivitySelector,
    AnalysisEngine,
    AnalysisSpec,
    AnalyticsError,
    ComparisonSpec,
    Measure,
    PopulationFilter,
)
from atus_pipeline.analytics.benchmarks import run_benchmarks
from atus_pipeline.api.app import create_app
from atus_pipeline.loading.loader import load_all
from atus_pipeline.serving.contract import LOCKED, READ, WRITE, missing_contract_columns
from tests.integration.analytics_dataset import analytics_settings
from tests.integration.conftest import create_scratch_database, drop_scratch_database

pytestmark = pytest.mark.integration

SLEEP = ActivitySelector(("010101",), label="Sleeping")
POPULATIONS = (
    PopulationFilter(),
    PopulationFilter(age_min=25, age_max=85),
    PopulationFilter(sex="female"),
    PopulationFilter(employment_status="employed"),
    PopulationFilter(has_household_children=True),
    PopulationFilter(region=3),
    PopulationFilter(state_fips="06"),
    PopulationFilter(education_level="bachelor_or_higher"),
    PopulationFilter(day_type="weekday"),
    PopulationFilter(diary_date_min=date(2023, 1, 1), diary_date_max=date(2023, 1, 31)),
)


@pytest.fixture(scope="module")
def contract(test_db_url, tmp_path_factory):
    url = create_scratch_database(test_db_url, "contract")
    settings = analytics_settings(tmp_path_factory.mktemp("contract"), url)
    with psycopg.connect(url) as conn:
        load_all(conn, settings)
    role = f"{urlsplit(url).path.lstrip('/')}_contract"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'DROP ROLE IF EXISTS "{role}"')
        conn.execute(f'CREATE ROLE "{role}" NOLOGIN')
        conn.execute(f'GRANT USAGE ON SCHEMA atus TO "{role}"')
        for privilege, tables in (("SELECT", READ), ("INSERT", WRITE)):
            for table, columns in tables.items():
                conn.execute(
                    f'GRANT {privilege} ({", ".join(columns)}) ON atus.{table} TO "{role}"'
                )
        for table in LOCKED:  # LOCK TABLE needs the table's own SELECT privilege
            conn.execute(f'GRANT SELECT ON atus.{table} TO "{role}"')
    yield url, role, settings
    drop_scratch_database(url)
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        conn.execute(f'DROP ROLE IF EXISTS "{role}"')


def as_role(url: str, role: str) -> str:
    return f"{url}?options={quote(f'-c role={role}')}"


def specs():
    for population in POPULATIONS:
        yield "estimate", AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, SLEEP, (2023,),
                                       population)
    yield "estimate", AnalysisSpec(Measure.PARTICIPATION_RATE, SLEEP, (2019, 2023),
                                   variance="none")
    yield "estimate", AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, SLEEP, (2019, 2020),
                                   weights="pandemic")
    yield "trend", AnalysisSpec(Measure.PARTICIPANTS_PER_DAY, SLEEP, (2019, 2020, 2023))
    yield "compare", ComparisonSpec(
        base=AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, SLEEP, (2023,)),
        group_a=PopulationFilter(sex="male"), group_b=PopulationFilter(sex="female"),
    )
    # error paths read the lexicon and the loaded years only
    yield "estimate", AnalysisSpec(
        Measure.AVERAGE_MINUTES_PER_DAY, ActivitySelector(("010198",)), (2023,)
    )
    yield "estimate", AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, SLEEP, (2011,))


def run_all(conn) -> list:
    engine = AnalysisEngine(conn)
    out = []
    for operation, spec in specs():
        try:
            out.append(getattr(engine, operation)(spec).to_dict())
        except AnalyticsError as exc:  # an expected refusal, compared as text
            out.append(f"{type(exc).__name__}: {exc}")
    return out


def test_the_migrated_schema_satisfies_the_contract(contract):
    url, _, _ = contract
    with psycopg.connect(url) as conn:
        assert missing_contract_columns(conn) == []
        conn.execute("ALTER TABLE atus.respondents DROP COLUMN household_children")
        assert missing_contract_columns(conn) == ["respondents.household_children"]
        conn.rollback()


def test_the_role_cannot_read_outside_the_contract(contract):
    url, role, _ = contract
    with psycopg.connect(as_role(url, role)) as conn:
        for sql in (
            "SELECT weekly_earnings FROM atus.respondents",
            "SELECT count(*) FROM atus.activity_companions",
            "SELECT start_time FROM atus.activities",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute(sql)


def test_the_engine_reads_only_contract_columns(contract):
    url, role, _ = contract
    with psycopg.connect(url) as owner:
        expected = run_all(owner)
    with psycopg.connect(as_role(url, role)) as restricted:
        assert run_all(restricted) == expected
    assert expected[-2].startswith("UnknownActivityError")
    assert expected[-1].startswith("InvalidSpecError")
    assert sum(isinstance(r, dict) for r in expected) >= len(POPULATIONS) + 2


def test_every_endpoint_works_within_the_contract(contract):
    url, role, settings = contract
    ops_token = "ops-" + "x" * 40
    restricted = type(settings)(
        database_url=as_role(url, role), data_dir=settings.data_dir,
        release=settings.release, log_level="WARNING", ops_token=ops_token,
    )
    analysis = {"activity": {"include": ["010101"]}, "years": [2023]}
    with TestClient(create_app(restricted)) as client:
        for path in (
            "/health", "/api/v1/meta", "/api/v1/activities",
            "/api/v1/activities?search=sleep", "/api/v1/activities/presets",
            "/api/v1/activities/01", "/api/v1/activities/010101",
            "/api/v1/population/metadata",
        ):
            assert client.get(path).status_code == 200, path
        # /ready is operator-only (ADR-012): it answers with the ops bearer.
        ready = client.get("/ready", headers={"Authorization": f"Bearer {ops_token}"})
        assert ready.status_code == 200, ready.text
        for path, body in (
            ("estimate", analysis),
            ("trend", {**analysis, "years": [2019, 2023]}),
            ("compare", {**analysis, "group_a": {"sex": "male"},
                         "group_b": {"sex": "female"}}),
        ):
            response = client.post(f"/api/v1/analysis/{path}", json=body)
            assert response.status_code == 200, (path, response.text)
        unknown = client.post("/api/v1/analysis/estimate",
                              json={**analysis, "activity": {"include": ["010198"]}})
        assert unknown.status_code == 422


def test_benchmarks_record_their_results_within_the_contract(contract):
    url, role, _ = contract
    with psycopg.connect(as_role(url, role)) as conn:
        results = run_benchmarks(conn)
        conn.commit()
    with psycopg.connect(url) as owner:
        recorded = owner.execute(
            "SELECT count(*) FROM atus.validation_results WHERE check_name LIKE 'analytics: %'"
        ).fetchone()[0]
    assert recorded == len(results)
