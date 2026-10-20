"""Zero-failure reload under live API traffic (audit F08/F19/F20, brief §15).

Starts the real API (uvicorn, real pool, real engine) on the fixture
database, keeps several clients reading continuously (metadata, the lexicon,
cached and uncached estimates, a trend), runs a full ``atus load`` of a second
data version in the middle, and checks that:

* every request succeeded (zero non-2xx, zero transport errors);
* the data version changed exactly once, together with the data;
* no response mixed old and new data, and no result was served or cached
  under the wrong version: every cached-or-computed estimate pairs the old
  analysis key with the old value or the new key with the new value
  (values for each version, and for every possible mix, are listed in
  ``reload_dataset.py``).

Before Phase 5 the same scenario blocked every DB-backed request for the
whole load and then failed them with 503s (``docs/operations.md`` 7.1).
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from itertools import count

import httpx
import psycopg
import pytest
import uvicorn

from atus_pipeline.api.app import create_app
from atus_pipeline.config import load_settings
from atus_pipeline.database.versions import read_data_version
from atus_pipeline.loading.loader import load_all
from tests.integration.analytics_dataset import analytics_settings
from tests.integration.conftest import create_scratch_database, drop_scratch_database
from tests.integration.reload_dataset import (
    MIXED_SLEEP_2023,
    V1_RESPONDENTS,
    V1_SLEEP_2023,
    V2_RESPONDENTS,
    V2_SLEEP_2023,
    make_second_version,
)

pytestmark = pytest.mark.integration

# Ports reserved for this workstream's servers (see the Phase 5 contract).
PORTS = range(8521, 8540)
READERS = 6
SLEEP_2023 = {"activity": {"include": ["010101"], "label": "Sleeping"}, "years": [2023]}
TREND = {"activity": {"include": ["010101"]}, "years": [2019, 2023], "variance": "none"}
# 2019 sleep, mean min/day: version 1 has P8 (360) and P9 (120), weights 1.5
# each -> 240; version 2 drops P8 -> 120.
V1_SLEEP_2019, V2_SLEEP_2019 = 240.0, 120.0
KINDS = ("meta", "estimate", "activities", "estimate_uncached", "trend", "estimate")

# Protection limits from ADR-012 (ignored by APIs that predate them), set
# high so this test exercises the reload, not the rate limiter.
GENEROUS_LIMITS = {
    "ATUS_API_RATE_ANALYSIS": "100000/60",
    "ATUS_API_RATE_REQUESTS": "100000/60",
    "ATUS_API_DAILY_ANALYSIS_CAP": "1000000",
    "ATUS_API_MAX_INFLIGHT": "16",
    "ATUS_API_MAX_INFLIGHT_HEAVY": "16",
    "ATUS_API_ADMISSION_WAIT_S": "60",
}


@dataclass
class Observation:
    kind: str
    started: float
    finished: float
    status: int | None = None
    x_cache: str = ""
    key: str = ""
    values: tuple = ()
    error: str = ""


@dataclass
class Traffic:
    base_url: str
    stop: threading.Event = field(default_factory=threading.Event)
    observations: list[Observation] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    unique: count = field(default_factory=lambda: count(1))

    def request(self, client: httpx.Client, kind: str) -> Observation:
        obs = Observation(kind, time.monotonic(), 0.0)
        try:
            if kind == "meta":
                response = client.get("/api/v1/meta")
            elif kind == "activities":
                response = client.get("/api/v1/activities", params={"level": 1})
            elif kind == "estimate":
                response = client.post("/api/v1/analysis/estimate", json=SLEEP_2023)
            elif kind == "estimate_uncached":
                # a unique confidence level makes a unique spec: always computed
                level = round(0.9 + next(self.unique) * 1e-6, 6)
                response = client.post(
                    "/api/v1/analysis/estimate", json={**SLEEP_2023, "confidence_level": level}
                )
            else:
                response = client.post("/api/v1/analysis/trend", json=TREND)
            obs.status = response.status_code
            obs.x_cache = response.headers.get("x-cache", "")
            obs.key = response.headers.get("x-analysis-key", "")
            body = response.json()
            if response.status_code == 200:
                if kind == "meta":
                    obs.values = (body["data"]["ingestion_run_id"], body["data"]["respondents"],
                                  tuple(body["data"]["years"]))
                elif kind == "activities":
                    obs.values = (len(body["activities"]),)
                elif kind == "trend":
                    obs.values = tuple(p["estimate"]["value"] for p in body["points"])
                else:
                    obs.values = (body["estimate"]["value"],)
        except Exception as exc:  # a transport failure is a failed request
            obs.error = f"{type(exc).__name__}: {exc}"
        obs.finished = time.monotonic()
        with self.lock:
            self.observations.append(obs)
        return obs

    def reader(self, offset: int) -> None:
        with httpx.Client(base_url=self.base_url, timeout=60) as client:
            i = offset
            while not self.stop.is_set():
                self.request(client, KINDS[i % len(KINDS)])
                i += 1

    def snapshot(self) -> list[Observation]:
        with self.lock:
            return list(self.observations)


def _free_port() -> int:
    for port in PORTS:
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    pytest.skip("no free port in this workstream's test range")


def _wait(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.01)


@pytest.fixture
def db_url(test_db_url):
    url = create_scratch_database(test_db_url, "apireload")
    yield url
    drop_scratch_database(url)


@pytest.fixture
def api(db_url, tmp_path, monkeypatch):
    """The real API on a real port against ``db_url`` (configured from the
    environment, like ``atus api``)."""
    monkeypatch.setenv("ATUS_DATABASE_URL", db_url)
    monkeypatch.setenv("ATUS_DATA_DIR", str(tmp_path / "api-data"))
    monkeypatch.setenv("ATUS_LOG_LEVEL", "WARNING")
    for name, value in GENEROUS_LIMITS.items():
        monkeypatch.setenv(name, value)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(load_settings()), host="127.0.0.1", port=port,
                       log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait(lambda: server.started, 30, "the API to start")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=30)


def test_reload_during_continuous_reads_fails_no_request(db_url, api, tmp_path):
    first = analytics_settings(tmp_path / "v1", db_url)
    second = analytics_settings(tmp_path / "v2", db_url)
    make_second_version(second)
    with psycopg.connect(db_url) as conn:
        load_all(conn, first)
        version_before = read_data_version(conn)

    traffic = Traffic(api)
    readers = [
        threading.Thread(target=traffic.reader, args=(offset,)) for offset in range(READERS)
    ]
    for thread in readers:
        thread.start()
    try:
        # enough traffic that every pooled connection has prepared statements
        _wait(lambda: len(traffic.snapshot()) >= 150, 60, "warm-up traffic")
        during_build: list[Observation] = []

        def keep_reading_while_built(conn, schema):
            # The new data is complete in the shadow but not live: reads go on
            # (the old TRUNCATE load blocked every DB-backed request here).
            mark = len(traffic.snapshot())
            _wait(lambda: len(traffic.snapshot()) >= mark + 60, 60, "reads during the load")
            during_build.extend(traffic.snapshot()[mark:])
            return []

        load_started = time.monotonic()
        with psycopg.connect(db_url) as conn:
            load_all(conn, second, validate=keep_reading_while_built)
            version_after = read_data_version(conn)
        load_finished = time.monotonic()

        def new_estimates_seen() -> bool:
            return sum(
                1 for o in traffic.snapshot()
                if o.kind == "estimate" and o.started > load_finished
                and o.values == (V2_SLEEP_2023,)
            ) >= 20

        _wait(new_estimates_seen, 60, "reads of the new data")
    finally:
        traffic.stop.set()
        for thread in readers:
            thread.join(timeout=60)

    observations = traffic.snapshot()
    failures = [o for o in observations if o.error or o.status != 200]
    assert not failures, failures[:5]

    # the load ran while requests were in flight
    overlapping = [o for o in observations
                   if o.started < load_finished and o.finished > load_started]
    assert len(overlapping) >= READERS

    assert version_after != version_before
    # while the load was in progress, readers were served the old data
    assert {o.kind for o in during_build} >= {"meta", "estimate", "trend"}
    assert all(o.values[0] == V1_SLEEP_2023 for o in during_build if o.kind == "estimate")

    def ordered(kind):
        return sorted((o for o in observations if o.kind == kind), key=lambda o: o.started)

    # estimates served or stored under a key: the key's version and the value agree
    estimates = ordered("estimate")
    old_key, new_key = estimates[0].key, estimates[-1].key
    assert old_key and new_key and old_key != new_key
    allowed = {
        (old_key, V1_SLEEP_2023),
        (new_key, V2_SLEEP_2023),
    }
    for obs in estimates:
        if obs.x_cache in ("hit", "miss"):
            assert (obs.key, obs.values[0]) in allowed, obs
    # every estimate, cached or not, is the old or the new value: never a mix
    for obs in estimates + ordered("estimate_uncached"):
        assert obs.values[0] in (V1_SLEEP_2023, V2_SLEEP_2023), obs
        assert obs.values[0] not in MIXED_SLEEP_2023
    for obs in ordered("trend"):
        assert obs.values in ((V1_SLEEP_2019, V1_SLEEP_2023), (V2_SLEEP_2019, V2_SLEEP_2023)), obs

    # metadata never pairs one run with the other run's data
    metas = ordered("meta")
    old_run, new_run = metas[0].values[0], metas[-1].values[0]
    assert new_run > old_run
    years = (2019, 2020, 2023)
    consistent = ((old_run, V1_RESPONDENTS, years), (new_run, V2_RESPONDENTS, years))
    for obs in metas:
        assert obs.values in consistent, obs
    for obs in ordered("activities"):
        assert obs.values == (18,)

    # both versions were actually served, old before new
    assert estimates[0].values[0] == V1_SLEEP_2023
    assert estimates[-1].values[0] == V2_SLEEP_2023
