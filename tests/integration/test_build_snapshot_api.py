"""scripts/build_snapshot.py against the real API on the fixture database.

The API (the real FastAPI app, served by uvicorn on an ephemeral loopback
port) answers from the hand-computed analytics fixture loaded through the
real Phase 1 loader. The snapshot must hold every metadata response and the
requested analyses byte for byte as the API serves them, and must refuse an
analysis the API rejects.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
import uvicorn

import atus_pipeline.config as config
from atus_pipeline.api.app import create_app
from atus_pipeline.database.versions import read_data_version
from atus_pipeline.loading.loader import load_all
from tests.integration.analytics_dataset import analytics_settings

pytestmark = pytest.mark.integration

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_snapshot.py"
_spec = importlib.util.spec_from_file_location("build_snapshot_integration", SCRIPT)
assert _spec is not None and _spec.loader is not None
snap = importlib.util.module_from_spec(_spec)
sys.modules["build_snapshot_integration"] = snap
_spec.loader.exec_module(snap)

ESTIMATE = "/api/v1/analysis/estimate"
COMPARE = "/api/v1/analysis/compare"
TREND = "/api/v1/analysis/trend"
# Bodies as the SPA's stable serializer writes them (sorted keys, no spaces).
REQUESTS = [
    (ESTIMATE, '{"activity":{"include":["010101"],"label":"Sleeping"},"years":[2023]}'),
    (TREND, '{"activity":{"include":["010101"]},"years":[2019,2023]}'),
    (COMPARE, '{"activity":{"include":["010101"]},"group_a":{"sex":"male"},'
              '"group_b":{"sex":"female"},"years":[2023]}'),
]
BUILD = "fixture0build0123"


@pytest.fixture(scope="module")
def live_api(tmp_path_factory, migrated_db) -> Iterator[tuple[str, str]]:
    settings = analytics_settings(tmp_path_factory.mktemp("snapshot-api"), migrated_db)
    if hasattr(config, "RateLimit"):
        # The snapshot build runs with generous per-client limits (documented in the script).
        settings = dataclasses.replace(
            settings,
            api_rate_analysis=config.RateLimit(1000, 60.0),
            api_rate_requests=config.RateLimit(100_000, 60.0),
        )
    with psycopg.connect(settings.database_url) as conn:
        load_all(conn, settings)
        data_version = read_data_version(conn)
    server = uvicorn.Server(uvicorn.Config(
        create_app(settings), host="127.0.0.1", port=0, log_level="warning",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            pytest.fail("the API did not start")
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", data_version
    server.should_exit = True
    thread.join(timeout=30)


def fetch(base: str, path: str, body: str | None = None) -> tuple[bytes, dict[str, str]]:
    request = urllib.request.Request(
        base + path,
        data=None if body is None else body.encode(),
        method="GET" if body is None else "POST",
        headers={"Accept-Encoding": "identity", "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=60) as response:
        return response.read(), {k.lower(): v for k, v in response.headers.items()}


def write_requests(tmp_path: Path, items: list[tuple[str, str]]) -> Path:
    path = tmp_path / "requests.json"
    path.write_text(json.dumps([{"path": p, "body": b} for p, b in items]))
    return path


def test_snapshots_every_response_byte_for_byte(live_api, tmp_path, monkeypatch, capsys):
    base, data_version = live_api
    monkeypatch.delenv("EDGE_AUTH_SECRET", raising=False)
    out = tmp_path / "snap"
    code = snap.main([
        "--api-base", base, "--out", str(out),
        "--requests", str(write_requests(tmp_path, REQUESTS)),
        "--api-build", BUILD, "--data-version", data_version,
    ])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    sid = f"{BUILD[:12]}-{hashlib.sha256(data_version.encode()).hexdigest()[:16]}"
    assert captured.out.strip() == sid
    assert snap.main(["--verify", str(out)]) == 0

    root = out / sid
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["data_version"] == data_version
    assert manifest["api_version"] == "v1"

    # Every lexicon code has its detail file.
    activities = json.loads(fetch(base, "/api/v1/activities")[0])
    codes = {item["code"] for item in activities["activities"]}
    assert {f"/api/v1/activities/{code}" for code in codes} <= set(manifest["get"])
    assert len(manifest["get"]) == len(codes) + 4

    for path, file in manifest["get"].items():
        assert (root / file).read_bytes() == fetch(base, path)[0], path

    assert len(manifest["post"]) == len(REQUESTS)
    for path, body in REQUESTS:
        k = hashlib.sha256(f"{path}?\n{body}".encode()).hexdigest()
        entry = manifest["post"][k]
        live, headers = fetch(base, path, body)
        assert entry["path"] == path
        assert (root / entry["file"]).read_bytes() == live
        assert entry["analysis_key"] == headers["x-analysis-key"]
        assert entry["x_cache"] == "miss"
        # The prewarmed bytes parse to a real result (the hand-computed sleep estimate).
        if path == ESTIMATE:
            assert json.loads(live)["estimate"]["value"] == pytest.approx(75.0)


def test_refuses_an_analysis_the_api_rejects(live_api, tmp_path, capsys):
    base, data_version = live_api
    out = tmp_path / "snap"
    bad = (ESTIMATE, '{"activity":{"include":["999999"]},"years":[2023]}')
    code = snap.main([
        "--api-base", base, "--out", str(out),
        "--requests", str(write_requests(tmp_path, [REQUESTS[0], bad])),
        "--api-build", BUILD, "--data-version", data_version,
    ])
    assert code == 1
    assert "HTTP 422" in capsys.readouterr().err
    assert list(out.iterdir()) == []
