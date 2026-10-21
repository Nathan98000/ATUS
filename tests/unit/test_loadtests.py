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


def test_production_api_environments_pass_the_api_checks(tmp_path):
    """The env files the tools hand to production-mode API containers are
    accepted by the API's own validation: their databases have no TLS, so
    they carry the local-stack escape hatch (and need it)."""
    from atus_pipeline.config import ConfigError, check_api_settings, load_settings

    cold_start = _load("cold_start", "atus_loadtests_cold_start")
    stage = runner.Stage(tmp_path, "unit")
    lt.write_private(stage.env_path, "ATUS_STACK_API_DB_PASSWORD=stack-db-password-1\n"
                     f"ATUS_STACK_EDGE_SECRET={'e' * 40}\nATUS_STACK_OPS_TOKEN={'o' * 40}\n")
    cold_env = lt.read_env_file(cold_start.container_env(stage, {"data_version": "v"}))

    args = Namespace(baseline_port=8899, no_pg_log=True, daily_cap=runner.LOAD_CAP,
                     max_parallel_workers=None, statement_timeout_ms=None, api_env=[],
                     api_cpus=None, api_memory=None)
    baseline = runner.BaselineTarget(stage, args)
    baseline.write_env(None, superuser=False)
    baseline_env = lt.read_env_file(baseline.env_path)

    for env in (cold_env, baseline_env):
        assert env["ATUS_ENV"] == "production"
        check_api_settings(load_settings(env))
        without_hatch = {k: v for k, v in env.items() if k != "ATUS_DATABASE_TLS"}
        with pytest.raises(ConfigError, match="sslmode"):
            load_settings(without_hatch)


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


def test_cold_start_ab_alternates_images_and_leaves_warmups_out():
    cold_start = _load("cold_start", "atus_loadtests_cold_start")
    plan = cold_start.run_plan(["a", "b"], runs=2, warmup=1)
    assert plan == [("a", True), ("b", True), ("a", False), ("b", False),
                    ("a", False), ("b", False)]
    runs = [{"warmup": True, "to_first_health_s": 99.0},
            {"warmup": False, "to_first_health_s": 2.0},
            {"warmup": False, "to_first_health_s": 4.0}]
    summary = cold_start.summaries(runs)["summary_to_first_health_s"]
    assert (summary["n"], summary["min"], summary["p50"], summary["max"]) == (2, 2.0, 3.0, 4.0)


def test_other_workloads_are_recognised_by_command_line():
    hits = ["/x/.venv/bin/python scripts/equivalence/replay.py --all",
            "/x/.venv/bin/python -m pytest tests/unit", "node /x/playwright/cli.js test",
            "/opt/homebrew/bin/k6 run x.js", "k6 run --quiet y.js", "node /x/wrangler dev"]
    misses = ["python3 loadtests/run.py profile --set EXECUTOR=duration",
              "cat loadtests/k6/uncached.js", "postgres: atus atus 172.18.0.3 idle"]
    assert all(lt.OTHER_WORKLOADS.search(cmd) for cmd in hits)
    assert not any(lt.OTHER_WORKLOADS.search(cmd) for cmd in misses)


def test_background_noise_is_summarised_per_container(tmp_path):
    path = tmp_path / "bg.jsonl"
    path.write_text('{"ts": 1, "name": "a", "cpu_pct": 1.0, "mem_mib": 5}\n'
                    '{"ts": 2, "name": "a", "cpu_pct": 3.0, "mem_mib": 5}\n'
                    '{"ts": 1, "name": "b", "cpu_pct": 0.5, "mem_mib": 1}\n')
    summary = lt.summarize_background(path)
    assert summary["cpu_pct_max"] == {"a": 3.0, "b": 0.5}
    assert summary["cpu_pct_sum_of_means"] == 2.5
    assert summary["samples_per_container"] == 2


report = _load("report", "atus_loadtests_report")


def test_report_reads_exported_rows_like_raw_ones(tmp_path):
    rows = [{"t": "req", "ph": "main", "ep": "estimate", "kind": "W1", "spec": "W1", "ts": 5,
             "st": 200, "code": "", "cache": "miss", "dur": 1500.5, "wait": 1500.0, "db": 1490.0,
             "app": 2.0, "bytes": 900, "ra": "", "vu": 1, "it": 0},
            {"t": "req", "ph": "main", "ep": "estimate", "kind": "W1", "spec": "W1", "ts": 9,
             "st": 503, "code": "overloaded", "cache": "", "dur": 5001.0, "wait": 5000.0,
             "db": None, "app": 5000.0, "bytes": 200, "ra": "10", "vu": 2, "it": 1}]
    raw = tmp_path / "raw" / "s"
    raw.mkdir(parents=True)
    with gzip.open(raw / "requests.jsonl.gz", "wt") as handle:
        handle.write("\n".join(__import__("json").dumps(r) for r in rows) + "\n")
    exported = tmp_path / "exp"
    exported.mkdir()
    exporter = _load("export_raw", "atus_loadtests_export_raw")
    import csv
    with gzip.open(exported / "requests.csv.gz", "wt", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=exporter.REQUEST_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({**r, "t_ms": r["ts"] - 5})
    for record_set in (report.load_records(raw), report.load_records(exported / "")):
        assert [(r["st"], r["code"], r["dur"], r["db"]) for r in record_set] == [
            (200, "", 1500.5, 1490.0), (503, "overloaded", 5001.0, None)]
    assert report.kinds_table(raw.parent, "s") == report.kinds_table(tmp_path, "exp")


def test_throttled_share_and_the_statement_timeout_clamp(tmp_path):
    assert report.throttled({"nr_periods": 200, "nr_throttled": 50}) == "25%"
    assert report.throttled({}) == "-"
    import json
    for name, p99_ms in (("floor", 70520.0), ("ceiling", 3030.0), ("mid", 12070.0)):
        (tmp_path / name).mkdir()
        (tmp_path / name / "analysis.json").write_text(json.dumps({
            "timed": True, "by_endpoint": {},
            "overall": {"requests": 30, "status": {"200": 30}, "error_codes": {},
                        "latency_ms": {"n": 30, "p99": p99_ms, "mean": 1.0}}}))
    # ADR-012: clamp(3 x p99, 30 s, 85 s)
    assert report.calibration(tmp_path, "floor") == {"p99_s": 70.52, "three_p99_s": 211.6,
                                                     "clamped_s": 85.0}
    assert report.calibration(tmp_path, "ceiling")["clamped_s"] == 30.0
    assert report.calibration(tmp_path, "mid")["clamped_s"] == 36.2


def test_lane_classes_follow_the_api_rule_for_the_harness_specs(tmp_path):
    """report.py --lanes: the API's rule (len(years), doubled for a compare, >= 8) on the
    harness's specs, which never fall between 2 and 7 year queries (k6/lib/specs.js)."""
    from atus_pipeline.api.protection import is_heavy

    cases = [  # (endpoint, spec id, years the harness sends)
        ("estimate", "errors-weird-020", 1), ("estimate", "gen1-3", 1),
        ("estimate", "pandemic-sleep-2020", 1), ("estimate", "starter-sleep", 1),
        ("compare", "genc-7", 1), ("compare", "compare-sex-sleep", 1),
        ("compare", "starter-tv-by-sex", 1),
        ("estimate", "genp-4", 8), ("estimate", "W1-pooled22-leisure", 22),
        ("estimate", "W4-F66-pooled22-tier1", 22), ("trend", "gent-2", 23),
        ("trend", "trend-sleep", 23), ("trend", "starter-leisure-trend", 23),
        ("trend", "W2-trend23-12", 23), ("compare", "W6-F66-compare22-sex", 22),
    ]
    for endpoint, spec, n_years in cases:
        expected = "heavy" if is_heavy(endpoint, range(n_years)) else "light"
        assert report.lane_class({"ep": endpoint, "spec": spec}) == f"{expected} {endpoint}"

    import json
    rows = [  # only main-phase 200 misses count
        {"ph": "main", "ep": "estimate", "spec": "gen1-1", "st": 200, "cache": "miss",
         "dur": 1300.0, "db": 1200.0},
        {"ph": "main", "ep": "estimate", "spec": "gen1-2", "st": 200, "cache": "miss",
         "dur": 900.0, "db": 800.0},
        {"ph": "main", "ep": "trend", "spec": "gent-1", "st": 200, "cache": "miss",
         "dur": 21000.0, "db": 20900.0},
        {"ph": "main", "ep": "estimate", "spec": "gen1-1", "st": 200, "cache": "hit",
         "dur": 2.0, "db": 0.0},
        {"ph": "main", "ep": "trend", "spec": "gent-2", "st": 503, "cache": "",
         "dur": 5000.0, "db": None},
        {"ph": "setup", "ep": "estimate", "spec": "starter-sleep", "st": 200, "cache": "miss",
         "dur": 700.0, "db": 690.0},
    ]
    run = tmp_path / "s"
    run.mkdir()
    with gzip.open(run / "requests.jsonl.gz", "wt") as handle:
        handle.write("\n".join(json.dumps({"t": "req", **r}) for r in rows) + "\n")
    table = report.lanes_table([("run", tmp_path, "s")])
    assert table[2:] == ["| run | light estimate | 2 | 0.80 | 1.00 | 1.18 | 1.20 | 1.30 |",
                         "| run | heavy trend | 1 | 20.90 | 20.90 | 20.90 | 20.90 | 21.00 |"]
