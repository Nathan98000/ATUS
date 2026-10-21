"""The load-test tooling's pure parts (loadtests/; brief §13).

The orchestration itself needs Docker and k6 and is exercised by its dry run
(loadtests/README.md); these tests pin what turns raw output into numbers:
the PostgreSQL log attribution, the per-request statistics, the functional
(latency-free) record of a dry run, and the guards that keep secrets out of
bundles and arguments.
"""

from __future__ import annotations

import gzip
import importlib.util
import sys
from argparse import Namespace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LT_DIR = REPO_ROOT / "loadtests"


def _load(name: str, module_name: str):
    sys.path.insert(0, str(LT_DIR))
    try:
        spec = importlib.util.spec_from_file_location(module_name, LT_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(LT_DIR))


lt = _load("ltcommon", "ltcommon")  # run.py imports it under this name
runner = _load("run", "atus_loadtests_run")

_D = "2026-10-21 02:56:0"
_LINES = [  # (seconds, pid, application, level, message)
    ("6.600", 90, "atus-lt-marker", "LOG", "duration: 0.1 ms  statement: SELECT 'MARK b.s BEGIN'"),
    ("6.700", 82, "atus-api", "LOG", "duration: 0.5 ms  statement: BEGIN READ ONLY"),
    ("6.710", 82, "atus-api", "LOG", "duration: 1.0 ms  parse <unnamed>: WITH activity_minutes"),
    None,  # a continuation line of the statement above
    ("6.720", 82, "atus-api", "LOG", "duration: 2.0 ms  bind <unnamed>: WITH activity_minutes"),
    ("6.720", 82, "atus-api", "DETAIL", "Parameters: $1 = '2025'"),
    ("9.000", 82, "atus-api", "LOG", "duration: 2280.0 ms  execute S_1: WITH activity_minutes"),
    ("9.100", 83, "atus-api", "ERROR", "canceling statement due to statement timeout"),
    ("9.200", 99, "psql", "LOG", "duration: 50.0 ms  statement: SELECT 1"),
    ("9.300", 82, "atus-api", "LOG", "duration: 0.2 ms  statement: COMMIT"),
    ("9.400", 90, "atus-lt-marker", "LOG", "duration: 0.1 ms  statement: SELECT 'MARK b.s END'"),
    ("9.500", 82, "atus-api", "LOG", "duration: 9.0 ms  statement: SELECT after_the_end"),
]
LOG = "\n".join("\tSELECT 1" if item is None else
                 f"{_D}{item[0]} UTC [{item[1]}] [{item[2]}] {item[3]}:  {item[4]}"
                 for item in _LINES)


def test_percentile_interpolates_between_ranks():
    assert lt.percentile([1, 2, 3, 4], 0.5) == 2.5
    assert lt.percentile([10], 0.99) == 10
    assert lt.percentile([], 0.5) is None
    assert lt.summarize([None, 1.0, 3.0])["n"] == 2


def test_db_time_is_the_api_sessions_between_the_markers():
    result = lt.attribute_db_time(lt.parse_pg_log(LOG.splitlines()), "b.s")
    assert result["complete"] is True
    # psql's 50 ms and everything after END are excluded; parse+bind+execute+BEGIN+COMMIT kept
    assert result["db_ms"] == pytest.approx(0.5 + 1.0 + 2.0 + 2280.0 + 0.2)
    assert result["statements"] == 3  # BEGIN, execute, COMMIT
    assert result["by_kind_ms"]["execute"] == 2280.0
    assert result["analysis_sql_ms"] == pytest.approx(1.0 + 2.0 + 2280.0)
    assert result["statement_timeouts"] == 1
    assert result["backends"] == 1


def test_without_an_application_name_only_marker_sessions_are_excluded():
    plain = "\n".join(line.replace(" [atus-api]", "").replace(" [psql]", "")
                      .replace(" [atus-lt-marker]", "") for line in LOG.splitlines())
    result = lt.attribute_db_time(lt.parse_pg_log(plain.splitlines()), "b.s", app=None)
    assert result["db_ms"] == pytest.approx(2283.7 + 50.0)  # psql counts: no way to tell


def test_windows_split_db_time_by_when_statements_finished():
    start = lt.log_epoch("2026-10-21 02:56:06.000 UTC")
    middle = lt.log_epoch("2026-10-21 02:56:08.000 UTC")
    end = lt.log_epoch("2026-10-21 02:56:10.000 UTC")
    result = lt.attribute_db_time(lt.parse_pg_log(LOG.splitlines()), "b.s",
                                  windows={"setup": (start, middle), "main": (middle, end)})
    assert result["windows"]["setup"]["db_ms"] == pytest.approx(3.5)
    assert result["windows"]["main"]["db_ms"] == pytest.approx(2280.2)
    assert result["windows"]["main"]["statements"] == 2


def _record(ts, dur, st=200, ep="estimate", cache="miss", phase="main", code="", db=10.0):
    return {"t": "req", "ph": phase, "ep": ep, "kind": ep, "ts": ts, "dur": dur, "st": st,
            "code": code, "cache": cache, "db": db, "app": 1.0, "ok": st == 200}


def test_request_statistics_exclude_setup_and_report_throughput():
    records = [_record(0, 1000), _record(1000, 1000), _record(2000, 2000, st=503,
               code="overloaded", cache=""), _record(-5000, 50, phase="setup")]
    analysis = lt.analyze_requests(records, with_latency=True)
    overall = analysis["overall"]
    assert overall["requests"] == 3
    assert overall["status"] == {"200": 2, "503": 1}
    assert overall["error_codes"] == {"overloaded": 1}
    assert overall["window_s"] == 4.0
    assert overall["throughput_rps"] == 0.75
    assert overall["throughput_200_rps"] == 0.5
    assert overall["latency_ms"]["max"] == 2000
    assert analysis["setup"]["requests"] == 1
    assert set(analysis["by_endpoint_cache"]) == {"estimate|miss", "estimate|none"}


def test_a_dry_run_record_carries_no_latency():
    analysis = lt.analyze_requests([_record(0, 5), _record(5, 7)], with_latency=True)
    functional = lt.strip_latency(analysis)
    text = repr(functional)
    for key in ("latency", "throughput", "window_s", "server_db", "server_app"):
        assert key not in text
    assert functional["overall"]["requests"] == 2
    assert "latency_ms" not in lt.analyze_requests([_record(0, 5)], with_latency=False)["overall"]


def test_secret_scan_reads_plain_and_gzipped_files(tmp_path):
    secret = "s3cr3t-value-0123456789"
    (tmp_path / "a.json").write_text('{"x": 1}')
    with gzip.open(tmp_path / "b.txt.gz", "wt") as handle:
        handle.write(f"token={secret}")
    (tmp_path / "stack.env").write_text(f"X={secret}")  # private env files are not bundle data
    assert lt.secret_scan(tmp_path, [secret, "short"]) == [str(tmp_path / "b.txt.gz")]


def test_container_environment_is_redacted():
    env = lt.safe_env(["ATUS_DATABASE_URL=postgresql://u:pw@db/atus", "ATUS_EDGE_SECRETS=x",
                       "ATUS_OPS_TOKEN=y", "ATUS_API_DAILY_ANALYSIS_CAP=10", "PATH=/bin",
                       "WEB_CONCURRENCY=1"])
    assert env == {"ATUS_DATABASE_URL": "<redacted>", "ATUS_EDGE_SECRETS": "<redacted>",
                   "ATUS_OPS_TOKEN": "<redacted>", "ATUS_API_DAILY_ANALYSIS_CAP": "10",
                   "WEB_CONCURRENCY": "1"}


def test_k6_settings_never_carry_the_secret_or_the_target():
    assert runner.parse_sets(["VUS=4", "DURATION=60s"]) == {"VUS": "4", "DURATION": "60s"}
    for bad in ("ATUS_LT_EDGE_SECRET=x", "BASE_URL=http://example.org", "vus=1", "VUS"):
        with pytest.raises(lt.LoadTestError):
            runner.parse_sets([bad])


def test_api_settings_take_atus_api_variables_only():
    args = Namespace(daily_cap=runner.LOAD_CAP, max_parallel_workers=0,
                     statement_timeout_ms=300000, api_env=["ATUS_API_MAX_INFLIGHT=3"],
                     api_cpus=0.1, api_memory="512m")
    env = runner.api_settings(args)["env"]
    assert env == {"ATUS_API_DAILY_ANALYSIS_CAP": "10000000",
                   "ATUS_API_MAX_PARALLEL_WORKERS": "0",
                   "ATUS_API_STATEMENT_TIMEOUT_MS": "300000", "ATUS_API_MAX_INFLIGHT": "3"}
    for bad in ("ATUS_DATABASE_URL=x", "ATUS_API_SECRET=x", "PATH=/x"):
        args.api_env = [bad]
        with pytest.raises(lt.LoadTestError):
            runner.api_settings(args)


def test_sweeps_and_dry_runs_expand_per_scenario():
    args = Namespace(scenarios="uncached,mix", set=["VUS=2"], sweep="VUS=1,4", dry_run=True)
    plan = runner.scenario_plan(args)
    assert [label for _, label, _ in plan] == ["uncached-vus1", "uncached-vus4",
                                               "mix-vus1", "mix-vus4"]
    assert plan[1][2]["VUS"] == "4" and plan[1][2]["ITERATIONS"] == "3"
    with pytest.raises(lt.LoadTestError):
        runner.scenario_plan(Namespace(scenarios="nope", set=None, sweep=None, dry_run=False))


def test_every_scenario_has_a_script_and_a_dry_run_size():
    assert set(runner.DRY_RUN) == set(runner.SCENARIOS)
    for script in runner.SCENARIOS.values():
        assert (LT_DIR / "k6" / script).is_file()


def test_raw_output_stays_out_of_the_repository(tmp_path):
    with pytest.raises(lt.LoadTestError):
        runner.Stage(REPO_ROOT / "loadtests" / "results", "x")
    stage = runner.Stage(tmp_path, "s1")
    assert stage.dir == tmp_path / "s1"
    with pytest.raises(lt.LoadTestError):
        runner.Stage(tmp_path, "Bad Name")
