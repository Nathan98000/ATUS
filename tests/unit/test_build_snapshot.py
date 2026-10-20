"""scripts/build_snapshot.py against an in-process fake API (no database).

The integration test (tests/integration/test_build_snapshot_api.py) runs it
against the real API on the fixture database; these tests cover the layout,
the request hash and snapshot id, secrets handling, refusal of errors,
retries, version consistency and manifest verification.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import shutil
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_snapshot.py"
_spec = importlib.util.spec_from_file_location("build_snapshot", SCRIPT)
assert _spec is not None and _spec.loader is not None
snap = importlib.util.module_from_spec(_spec)
sys.modules["build_snapshot"] = snap  # dataclasses resolve annotations through it
_spec.loader.exec_module(snap)

SECRET = "edge-secret-0123456789abcdef0123456789abcdef"
DV = "0325:serving-20361020T000000Z-0a1b2c3d:0123456789ab"
BUILD = "abcdef0123456789abcdef"
ESTIMATE = "/api/v1/analysis/estimate"
TREND = "/api/v1/analysis/trend"
BODY_A = '{"activity":{"preset":"sleep"},"years":[2024]}'
BODY_B = '{"activity":{"preset":"sleep"},"years":[2023,2024]}'
#: Exact bytes a JSON round trip would change: the snapshot must keep them.
RESULT = '{"estimate":{"value":1.50, "se":2e-7},"spec":{"years":[2024]}}'

Reply = tuple[int, dict[str, str], bytes]


@dataclass
class FakeApi:
    """A tiny HTTP API: routes map (method, path) to a reply or a callable."""

    routes: dict[tuple[str, str], Reply | Callable[[dict], Reply]] = field(default_factory=dict)
    calls: list[dict] = field(default_factory=list)
    base: str = ""

    def reply(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> Reply:
        self.calls.append({"method": method, "path": path, "body": body, "headers": headers})
        route = self.routes.get((method, path))
        if route is None:
            return error(404, "not_found")
        return route({"body": body, "headers": headers}) if callable(route) else route


def ok(payload: str | bytes, **headers: str) -> Reply:
    body = payload.encode() if isinstance(payload, str) else payload
    return 200, {"Content-Type": "application/json", "X-Data-Version": DV,
                 "X-Analytics-Version": "0.2", **headers}, body


def analysis(payload: str = RESULT, **headers: str) -> Reply:
    return ok(payload, **{"X-Cache": "miss", "X-Analysis-Key": "f" * 64,
                          "X-Response-Format": "1", **headers})


def error(status: int, code: str, **headers: str) -> Reply:
    body = json.dumps({"error": {"code": code, "message": "x", "details": None}}).encode()
    return status, {"Content-Type": "application/json", **headers}, body


def default_routes() -> dict:
    meta = json.dumps({"api_version": "v1", "analytics_version": "0.2",
                       "data": {"years": [2023, 2024]}})
    activities = json.dumps({"activities": [
        {"code": "01", "name": "Personal care", "level": 1},
        {"code": "0101", "name": "Sleeping", "level": 2},
        {"code": "010101", "name": "Sleeping", "level": 3},
    ]})
    routes: dict = {
        ("GET", "/health"): (200, {"Content-Type": "application/json"},
                             json.dumps({"status": "ok", "build": BUILD}).encode()),
        ("GET", "/api/v1/meta"): ok(meta),
        ("GET", "/api/v1/activities"): ok(activities),
        ("GET", "/api/v1/activities/presets"): ok('{"presets": []}'),
        ("GET", "/api/v1/population/metadata"): ok('{"dimensions": {}}'),
        ("POST", ESTIMATE): analysis(),
        ("POST", TREND): analysis('{"points":[]}'),
    }
    for code in ("01", "0101", "010101"):
        routes[("GET", f"/api/v1/activities/{code}")] = ok(json.dumps({"code": code}))
    return routes


@pytest.fixture
def api() -> Iterator[FakeApi]:
    fake = FakeApi(default_routes())

    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            headers = {name.lower(): value for name, value in self.headers.items()}
            status, reply_headers, reply = fake.reply(self.command, self.path, body, headers)
            self.send_response(status)
            for name, value in reply_headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        do_GET = do_POST = _handle

        def log_message(self, *args: object) -> None:  # silence the test output
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    fake.base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    yield fake
    server.shutdown()
    server.server_close()


@pytest.fixture
def secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "edge.env"
    path.write_text(f"# generated\nOPS_TOKEN=other\nEDGE_AUTH_SECRET={SECRET}\n")
    path.chmod(0o600)
    return path


def request_list(tmp_path: Path, *items: dict) -> Path:
    path = tmp_path / f"requests-{len(list(tmp_path.glob('requests-*')))}.json"
    path.write_text(json.dumps(list(items)))
    return path


def key(path: str, body: str, query: str = "") -> str:
    return hashlib.sha256(f"{path}?{query}\n{body}".encode()).hexdigest()


def run(api: FakeApi, tmp_path: Path, secret_file: Path | None, *extra: str) -> int:
    args = ["--api-base", api.base, "--out", str(tmp_path / "snap")]
    if secret_file is not None:
        args += ["--edge-secret-file", str(secret_file)]
    return snap.main([*args, *extra])


EXPECTED_ID = f"{BUILD[:12]}-{hashlib.sha256(DV.encode()).hexdigest()[:16]}"


class TestHashes:
    def test_request_key_is_sha256_of_path_query_newline_body(self):
        assert snap.request_key(ESTIMATE, "", BODY_A.encode()) == key(ESTIMATE, BODY_A)
        assert snap.request_key(ESTIMATE, "a=1", b"{}") == key(ESTIMATE, "{}", "a=1")

    def test_request_key_matches_the_edge_worker_vector(self):
        # Same vector as edge/test/worker/cache.test.ts.
        body = b'{"activity":{"preset":"leisure"},"years":[2024]}'
        assert snap.request_key(ESTIMATE, "", body) == (
            "bc90fd68322385f99eb564468bc27e0e3a91011fa64a1178d4dbe4422f05f1e3"
        )

    def test_snapshot_id(self):
        assert snap.snapshot_id("0123456789abcdef", "x") == (
            "0123456789ab-" + hashlib.sha256(b"x").hexdigest()[:16]
        )


class TestBuild:
    def test_writes_the_contract_layout_with_exact_bytes(self, api, tmp_path, secret_file,
                                                         capsys):
        requests = request_list(
            tmp_path,
            {"name": "a", "path": ESTIMATE, "body": BODY_A, "key": key(ESTIMATE, BODY_A)},
            {"path": TREND, "body": BODY_B},
            {"path": ESTIMATE, "body": BODY_A},  # duplicate: fetched once
        )
        assert run(api, tmp_path, secret_file, "--requests", str(requests)) == 0
        captured = capsys.readouterr()
        assert captured.out.strip() == EXPECTED_ID

        out = tmp_path / "snap"
        pointer = json.loads((out / "current.json").read_text())
        assert pointer == {
            "snapshot_id": EXPECTED_ID,
            "data_version": DV,
            "analytics_version": "0.2",
            "api_version": "v1",
            "response_format": "1",
            "generated_at": pointer["generated_at"],
        }
        root = out / EXPECTED_ID
        manifest = json.loads((root / "manifest.json").read_text())
        assert manifest["get"] == {
            "/api/v1/activities": "get/activities.json",
            "/api/v1/activities/01": "get/activities/01.json",
            "/api/v1/activities/0101": "get/activities/0101.json",
            "/api/v1/activities/010101": "get/activities/010101.json",
            "/api/v1/activities/presets": "get/activities/presets.json",
            "/api/v1/meta": "get/meta.json",
            "/api/v1/population/metadata": "get/population/metadata.json",
        }
        ka, kb = key(ESTIMATE, BODY_A), key(TREND, BODY_B)
        assert manifest["post"] == {
            k: v for k, v in sorted({
                ka: {"path": ESTIMATE, "file": f"post/{ka}.json", "analysis_key": "f" * 64,
                     "x_cache": "miss"},
                kb: {"path": TREND, "file": f"post/{kb}.json", "analysis_key": "f" * 64,
                     "x_cache": "miss"},
            }.items())
        }
        assert (root / f"post/{ka}.json").read_bytes() == RESULT.encode()
        assert (root / f"post/{kb}.json").read_bytes() == b'{"points":[]}'
        assert (root / "get/activities/0101.json").read_bytes() == b'{"code": "0101"}'
        for name, entry in manifest["files"].items():
            body = (root / name).read_bytes()
            assert entry == {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
        assert manifest["api_build"] == BUILD[:12]
        # One POST per distinct request, with the exact body bytes.
        posts = [call for call in api.calls if call["method"] == "POST"]
        assert [call["body"] for call in posts] == [BODY_A.encode(), BODY_B.encode()]
        assert all(call["headers"]["content-type"] == "application/json" for call in posts)
        # No temporary directories are left in the published tree.
        assert sorted(path.name for path in out.iterdir()) == [EXPECTED_ID, "current.json"]

    def test_sends_the_edge_secret_only_as_a_header_and_never_prints_it(
        self, api, tmp_path, secret_file, capsys
    ):
        assert run(api, tmp_path, secret_file) == 0
        assert api.calls and all(
            call["headers"].get("x-atus-edge-auth") == SECRET for call in api.calls
        )
        assert all(call["headers"]["accept-encoding"] == "identity" for call in api.calls)
        captured = capsys.readouterr()
        assert SECRET not in captured.out + captured.err
        for path in (tmp_path / "snap").rglob("*"):
            if path.is_file():
                assert SECRET.encode() not in path.read_bytes()

    def test_takes_the_secret_from_a_bare_file_or_the_environment(self, api, tmp_path,
                                                                  monkeypatch):
        bare = tmp_path / "secret"
        bare.write_text(f"{SECRET}\n")
        assert run(api, tmp_path, bare) == 0
        assert api.calls[-1]["headers"]["x-atus-edge-auth"] == SECRET
        api.calls.clear()
        monkeypatch.setenv("EDGE_AUTH_SECRET", "from-the-environment")
        assert run(api, tmp_path, None) == 0
        assert api.calls[-1]["headers"]["x-atus-edge-auth"] == "from-the-environment"

    def test_sends_no_edge_header_without_a_secret(self, api, tmp_path, monkeypatch):
        monkeypatch.delenv("EDGE_AUTH_SECRET", raising=False)
        assert run(api, tmp_path, None) == 0
        assert all("x-atus-edge-auth" not in call["headers"] for call in api.calls)

    @pytest.mark.parametrize("content", ["a\nb\n", "OTHER=1\n", "EDGE_AUTH_SECRET=\n",
                                         "two words\n"])
    def test_refuses_an_unusable_secret_file(self, api, tmp_path, content, capsys):
        path = tmp_path / "bad.env"
        path.write_text(content)
        assert run(api, tmp_path, path) == 1
        assert api.calls == []
        assert "edge secret" in capsys.readouterr().err

    def test_takes_the_build_from_health_or_the_flag(self, api, tmp_path, secret_file, capsys):
        assert run(api, tmp_path, secret_file, "--api-build", "0123456789abcdef") == 0
        assert capsys.readouterr().out.startswith("0123456789ab-")
        api.routes[("GET", "/health")] = (200, {"Content-Type": "application/json"},
                                         b'{"status":"ok"}')
        assert run(api, tmp_path, secret_file) == 1
        assert "--api-build" in capsys.readouterr().err

    def test_decodes_a_gzip_body(self, api, tmp_path, secret_file):
        status, headers, body = analysis()
        api.routes[("POST", ESTIMATE)] = (status, {**headers, "Content-Encoding": "gzip"},
                                          gzip.compress(body))
        requests = request_list(tmp_path, {"path": ESTIMATE, "body": BODY_A})
        assert run(api, tmp_path, secret_file, "--requests", str(requests)) == 0
        stored = tmp_path / "snap" / EXPECTED_ID / "post" / f"{key(ESTIMATE, BODY_A)}.json"
        assert stored.read_bytes() == RESULT.encode()


class TestRefusals:
    def assert_nothing_published(self, tmp_path: Path) -> None:
        out = tmp_path / "snap"
        assert not (out / "current.json").exists()
        assert list(out.iterdir()) == []

    @pytest.mark.parametrize(
        ("reply", "code"),
        [
            (error(422, "validation_error"), "validation_error"),
            (error(500, "internal_error"), "internal_error"),
            (error(403, "forbidden_origin"), "forbidden_origin"),
        ],
    )
    def test_refuses_an_error_response(self, api, tmp_path, secret_file, capsys, reply, code):
        api.routes[("POST", ESTIMATE)] = reply
        requests = request_list(tmp_path, {"path": ESTIMATE, "body": BODY_A})
        assert run(api, tmp_path, secret_file, "--requests", str(requests)) == 1
        err = capsys.readouterr().err
        assert code in err and ESTIMATE in err
        self.assert_nothing_published(tmp_path)

    def test_refuses_an_analysis_computed_during_a_data_reload(self, api, tmp_path,
                                                                secret_file, capsys):
        # X-Cache: bypass is the API's answer computed while the data changed
        # under it: not tied to one data version (the edge cache refuses it too).
        api.routes[("POST", ESTIMATE)] = analysis(**{"X-Cache": "bypass"})
        requests = request_list(tmp_path, {"path": ESTIMATE, "body": BODY_A})
        assert run(api, tmp_path, secret_file, "--requests", str(requests)) == 1
        err = capsys.readouterr().err
        assert "X-Cache: bypass" in err and ESTIMATE in err
        self.assert_nothing_published(tmp_path)

    @pytest.mark.parametrize("x_cache", ["miss", "hit", "join"])
    def test_accepts_computed_cached_and_joined_analyses(self, api, tmp_path, secret_file,
                                                         x_cache):
        api.routes[("POST", ESTIMATE)] = analysis(**{"X-Cache": x_cache})
        requests = request_list(tmp_path, {"path": ESTIMATE, "body": BODY_A})
        assert run(api, tmp_path, secret_file, "--requests", str(requests)) == 0

    def test_refuses_an_html_page(self, api, tmp_path, secret_file, capsys):
        api.routes[("GET", "/api/v1/population/metadata")] = (
            200, {"Content-Type": "text/html"}, b"<!doctype html><title>SPA</title>")
        assert run(api, tmp_path, secret_file) == 1
        assert "text/html" in capsys.readouterr().err
        self.assert_nothing_published(tmp_path)

    def test_retries_429_and_503_with_retry_after(self, api, tmp_path, secret_file,
                                                  monkeypatch):
        waits: list[float] = []
        monkeypatch.setattr(snap.time, "sleep", waits.append)
        replies = [error(503, "overloaded", **{"Retry-After": "2"}),
                   error(429, "rate_limited", **{"Retry-After": "0"}), analysis()]
        api.routes[("POST", ESTIMATE)] = lambda _request: replies.pop(0)
        requests = request_list(tmp_path, {"path": ESTIMATE, "body": BODY_A})
        assert run(api, tmp_path, secret_file, "--requests", str(requests)) == 0
        assert waits == [2, 1]

    def test_gives_up_when_the_wait_budget_is_spent(self, api, tmp_path, secret_file,
                                                    monkeypatch, capsys):
        monkeypatch.setattr(snap.time, "sleep", lambda _s: None)
        api.routes[("POST", ESTIMATE)] = error(429, "rate_limited", **{"Retry-After": "40"})
        requests = request_list(tmp_path, {"path": ESTIMATE, "body": BODY_A})
        assert run(api, tmp_path, secret_file, "--requests", str(requests),
                   "--max-wait", "100") == 1
        posts = [call for call in api.calls if call["method"] == "POST"]
        assert len(posts) == 3  # waits 40 + 40, then 40 more would pass 100
        assert "rate_limited" in capsys.readouterr().err

    def test_refuses_mixed_data_versions(self, api, tmp_path, secret_file, capsys):
        api.routes[("GET", "/api/v1/activities/0101")] = ok(
            '{"code":"0101"}', **{"X-Data-Version": "other-release"})
        assert run(api, tmp_path, secret_file) == 1
        assert "disagree on data_version" in capsys.readouterr().err
        self.assert_nothing_published(tmp_path)

    def test_needs_the_data_version_flag_when_the_api_sends_none(self, api, tmp_path,
                                                                  secret_file, capsys):
        for route, (status, headers, body) in list(api.routes.items()):
            api.routes[route] = (status, {k: v for k, v in headers.items()
                                          if k != "X-Data-Version"}, body)
        assert run(api, tmp_path, secret_file) == 1
        assert "--data-version" in capsys.readouterr().err
        assert run(api, tmp_path, secret_file, "--data-version", DV) == 0
        assert capsys.readouterr().out.strip() == EXPECTED_ID

    def test_refuses_a_data_version_flag_the_api_contradicts(self, api, tmp_path, secret_file,
                                                             capsys):
        assert run(api, tmp_path, secret_file, "--data-version", "expected-release") == 1
        assert "not 'expected-release'" in capsys.readouterr().err

    def test_refuses_another_response_format(self, api, tmp_path, secret_file, capsys):
        api.routes[("POST", ESTIMATE)] = analysis(**{"X-Response-Format": "2"})
        requests = request_list(tmp_path, {"path": ESTIMATE, "body": BODY_A})
        assert run(api, tmp_path, secret_file, "--requests", str(requests)) == 1
        assert "response_format" in capsys.readouterr().err

    def test_never_follows_a_redirect(self, api, tmp_path, secret_file, capsys):
        api.routes[("GET", "/api/v1/meta")] = (
            302, {"Location": f"{api.base}/elsewhere", "Content-Type": "text/plain"}, b"")
        assert run(api, tmp_path, secret_file) == 1
        assert "HTTP 302" in capsys.readouterr().err
        assert all(call["path"] != "/elsewhere" for call in api.calls)

    @pytest.mark.parametrize(
        ("item", "message"),
        [
            ({"path": "/api/v1/meta", "body": "{}"}, "path must be one of"),
            ({"path": ESTIMATE, "body": {"years": [2024]}}, "exact request body"),
            ({"path": ESTIMATE, "body": BODY_A, "key": "0" * 64}, "disagree on the request hash"),
            ("not an object", "must be an object"),
        ],
    )
    def test_refuses_an_invalid_request_list(self, api, tmp_path, secret_file, capsys, item,
                                             message):
        requests = request_list(tmp_path, item)
        assert run(api, tmp_path, secret_file, "--requests", str(requests)) == 1
        assert message in capsys.readouterr().err
        assert api.calls == []

    @pytest.mark.parametrize(
        "base",
        ["http://example.com", "ftp://127.0.0.1", "http://user:pw@127.0.0.1:1",
         "https://example.com/?x=1"],
    )
    def test_refuses_an_unsafe_api_base(self, base):
        with pytest.raises(snap.SnapshotError):
            snap.validate_api_base(base)

    @pytest.mark.parametrize(
        "base", ["https://example.com", "http://127.0.0.1:8571", "http://localhost:1/",
                 "http://[::1]:8571"],
    )
    def test_accepts_https_and_loopback_http(self, base):
        assert snap.validate_api_base(base) == base.rstrip("/")


class TestVerify:
    def build(self, api: FakeApi, tmp_path: Path, secret_file: Path) -> Path:
        requests = request_list(tmp_path, {"path": ESTIMATE, "body": BODY_A})
        assert run(api, tmp_path, secret_file, "--requests", str(requests)) == 0
        return tmp_path / "snap"

    def test_verifies_a_built_snapshot(self, api, tmp_path, secret_file, capsys):
        out = self.build(api, tmp_path, secret_file)
        capsys.readouterr()
        assert snap.main(["--verify", str(out)]) == 0
        assert capsys.readouterr().out.strip() == EXPECTED_ID

    def test_detects_a_changed_byte(self, api, tmp_path, secret_file, capsys):
        out = self.build(api, tmp_path, secret_file)
        target = out / EXPECTED_ID / "post" / f"{key(ESTIMATE, BODY_A)}.json"
        target.write_bytes(target.read_bytes().replace(b"1.50", b"1.51"))
        assert snap.main(["--verify", str(out)]) == 1
        assert "sha256 does not match" in capsys.readouterr().err

    def test_detects_a_missing_or_extra_file(self, api, tmp_path, secret_file):
        out = self.build(api, tmp_path, secret_file)
        root = out / EXPECTED_ID
        (root / "get" / "meta.json").unlink()
        assert snap.main(["--verify", str(out)]) == 1
        shutil.rmtree(out)
        out = self.build(api, tmp_path, secret_file)
        (out / EXPECTED_ID / "get" / "extra.json").write_text("{}")
        assert snap.main(["--verify", str(out)]) == 1

    def test_detects_a_pointer_that_disagrees_with_the_manifest(self, api, tmp_path,
                                                                secret_file):
        out = self.build(api, tmp_path, secret_file)
        pointer = json.loads((out / "current.json").read_text())
        (out / "current.json").write_text(json.dumps({**pointer, "data_version": "x"}))
        assert snap.main(["--verify", str(out)]) == 1


FAKE_GENERATOR = r"""
// Stand-in for frontend/scripts/snapshot-requests.mjs: the same two passes.
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
const args = process.argv.slice(2)
const entry = (path, body) => ({
  name: path, path, body,
  key: createHash('sha256').update(`${path}?\n${body}`, 'utf8').digest('hex'),
})
if (args[0] === '--meta') {
  const meta = JSON.parse(readFileSync(args[1], 'utf8'))
  const years = meta.data.years
  process.stdout.write(JSON.stringify([
    entry('/api/v1/analysis/estimate',
      JSON.stringify({ years: [years[1]], variance: 'replicate' })),
    entry('/api/v1/analysis/trend', JSON.stringify({ years })),
  ]))
} else if (args[0] === '--canonical') {
  const responses = JSON.parse(readFileSync(args[1], 'utf8'))
  process.stdout.write(JSON.stringify(responses
    .filter((item) => item.response.spec)
    .map((item) => entry(item.path, JSON.stringify(item.response.spec)))))
} else {
  process.exit(2)
}
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
class TestRequestScript:
    def test_runs_both_passes_and_prewarms_the_canonical_specs(self, api, tmp_path,
                                                               secret_file):
        # The trend's canonical spec equals its request (no second POST); the
        # estimate's differs (prewarmed too, so share links of the result hit).
        api.routes[("POST", TREND)] = analysis('{"points":[],"spec":{"years":[2023,2024]}}')
        script = tmp_path / "generator.mjs"
        script.write_text(FAKE_GENERATOR)
        assert run(api, tmp_path, secret_file, "--requests-script", str(script)) == 0
        posts = [(call["path"], call["body"].decode()) for call in api.calls
                 if call["method"] == "POST"]
        assert posts == [
            (ESTIMATE, '{"years":[2024],"variance":"replicate"}'),
            (TREND, '{"years":[2023,2024]}'),
            (ESTIMATE, '{"years":[2024]}'),
        ]
        manifest = json.loads(
            (tmp_path / "snap" / EXPECTED_ID / "manifest.json").read_text())
        assert sorted(manifest["post"]) == sorted(key(path, body) for path, body in posts)

    def test_accepts_a_script_path_relative_to_the_working_directory(
        self, api, tmp_path, secret_file, monkeypatch
    ):
        # The documented CI call passes frontend/scripts/snapshot-requests.mjs
        # relative to the repository root, while the generator runs in the
        # build's temporary directory.
        scripts = tmp_path / "frontend" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "generator.mjs").write_text(FAKE_GENERATOR)
        monkeypatch.chdir(tmp_path)
        assert run(api, tmp_path, secret_file, "--requests-script",
                   "frontend/scripts/generator.mjs") == 0
        assert [call["path"] for call in api.calls if call["method"] == "POST"] == [
            ESTIMATE, TREND, ESTIMATE,
        ]

    def test_refuses_a_missing_script(self, api, tmp_path, secret_file, capsys):
        missing = tmp_path / "no-such-generator.mjs"
        assert run(api, tmp_path, secret_file, "--requests-script", str(missing)) == 1
        assert "no-such-generator.mjs is not a file" in capsys.readouterr().err

    def test_reports_the_error_of_a_generator_that_throws(self, api, tmp_path, secret_file,
                                                          capsys):
        # Node ends an uncaught error's output with its own version line.
        script = tmp_path / "throws.mjs"
        script.write_text("throw new Error('meta has no years')\n")
        assert run(api, tmp_path, secret_file, "--requests-script", str(script)) == 1
        err = capsys.readouterr().err
        assert "throws.mjs --meta failed: Error: meta has no years" in err

    def test_fails_when_the_generator_fails(self, api, tmp_path, secret_file, capsys):
        script = tmp_path / "broken.mjs"
        script.write_text("process.stderr.write('no meta.data.years\\n'); process.exit(2)\n")
        assert run(api, tmp_path, secret_file, "--requests-script", str(script)) == 1
        assert "no meta.data.years" in capsys.readouterr().err
