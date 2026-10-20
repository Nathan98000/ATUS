"""Functional smoke checks against a running API (or the edge in front of it).

Standard library only, so it runs inside the API image on the compose network
(the API port is never published on the host). Prints one line per check and
exits non-zero if any check fails. Never prints the edge secret. Functional
only: nothing here times anything.

    python smoke_probe.py --base-url http://api:10000 --fixture

Environment: ATUS_SMOKE_EDGE_SECRET is sent as ``X-ATUS-Edge-Auth`` on the
requests that model the edge (Worker -> API). Leave it unset when probing
through the edge, which adds the header itself.

Checks:
  health          GET /health is 200 {"status": "ok", ...} (no edge secret; liveness
                  must answer the platform's health checker directly)
  build           /health reports the expected build (--expect-build)
  meta            GET /api/v1/meta through the edge path is 200 with api_version
  analysis        POST /api/v1/analysis/estimate is 200; with --fixture, the
                  pencil-and-paper value (sleep 2023 = 75.0 minutes)
  error_envelope  an unknown activity gets 422 unknown_activity in the v1 error envelope
  origin_lock     the same /meta request without the edge secret is 403
                  forbidden_origin (a direct hit on the onrender.com origin)
  docs_off        /docs is not served in production

``--skip-protection-checks`` reports ``origin_lock`` and ``docs_off`` as SKIP;
it exists only for images built before the API protection work landed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

EDGE_AUTH_HEADER = "X-ATUS-Edge-Auth"
CLIENT_IP_HEADER = "X-ATUS-Client-IP"
SMOKE_CLIENT_IP = "203.0.113.7"  # TEST-NET-3 (RFC 5737): never a real visitor

ESTIMATE_BODY = {"activity": {"include": ["010101"], "label": "Sleeping"}, "years": [2023]}
UNKNOWN_ACTIVITY_BODY = {"activity": {"include": ["999999"]}, "years": [2023]}
FIXTURE_SLEEP_2023_MINUTES = 75.0  # tests/integration/api/test_api_endpoints.py


class Probe:
    def __init__(self, base_url: str, edge_secret: str | None, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.edge_secret = edge_secret
        self.timeout = timeout
        self.failures = 0

    def request(
        self, method: str, path: str, body: dict | None = None, *, as_edge: bool = True
    ) -> tuple[int, dict, dict | None]:
        """Send one request; return (status, lower-cased headers, JSON body or None)."""
        headers = {"Accept": "application/json", "X-Request-ID": f"smoke-{uuid.uuid4().hex}"}
        data = None
        if body is not None:
            data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        if as_edge and self.edge_secret:
            headers[EDGE_AUTH_HEADER] = self.edge_secret
            headers[CLIENT_IP_HEADER] = SMOKE_CLIENT_IP
        req = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status, raw, resp_headers = resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as err:
            status, raw, resp_headers = err.code, err.read(), err.headers
        lowered = {k.lower(): v for k, v in resp_headers.items()}
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        return status, lowered, payload

    def check(self, name: str, ok: bool, detail: str) -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<15} {detail}", flush=True)
        if not ok:
            self.failures += 1

    @staticmethod
    def skip(name: str, detail: str) -> None:
        print(f"[SKIP] {name:<15} {detail}", flush=True)


def is_error_envelope(payload: dict | None, code: str | None = None) -> bool:
    """The v1 error shape: {"error": {"code", "message", "details"}}."""
    if not isinstance(payload, dict) or set(payload) != {"error"}:
        return False
    error = payload["error"]
    if not isinstance(error, dict) or not {"code", "message", "details"} <= set(error):
        return False
    return code is None or error["code"] == code


def run(args: argparse.Namespace) -> int:
    probe = Probe(args.base_url, os.environ.get("ATUS_SMOKE_EDGE_SECRET") or None, args.timeout)

    status, _, body = probe.request("GET", "/health", as_edge=False)
    healthy = status == 200 and isinstance(body, dict) and body.get("status") == "ok"
    probe.check("health", healthy, f"status={status}")
    if args.expect_build:
        build = body.get("build") if isinstance(body, dict) else None
        probe.check("build", build == args.expect_build, f"build={build!r}")

    status, headers, body = probe.request("GET", "/api/v1/meta")
    api_version = body.get("api_version") if isinstance(body, dict) else None
    data_version = headers.get("x-data-version")
    probe.check(
        "meta",
        status == 200 and bool(api_version) and "x-request-id" in headers,
        f"status={status} api_version={api_version!r} data_version={data_version!r}",
    )

    status, headers, body = probe.request("POST", "/api/v1/analysis/estimate", ESTIMATE_BODY)
    value = (body or {}).get("estimate", {}).get("value") if isinstance(body, dict) else None
    ok = status == 200 and isinstance(value, (int, float))
    if args.fixture:
        ok = ok and abs(value - FIXTURE_SLEEP_2023_MINUTES) < 1e-9
    probe.check(
        "analysis", ok, f"status={status} value={value!r} x-cache={headers.get('x-cache')!r}"
    )

    status, headers, body = probe.request(
        "POST", "/api/v1/analysis/estimate", UNKNOWN_ACTIVITY_BODY
    )
    probe.check(
        "error_envelope",
        status == 422 and is_error_envelope(body, "unknown_activity"),
        f"status={status} code={(body or {}).get('error', {}).get('code')!r}",
    )

    if args.skip_protection_checks:
        probe.skip("origin_lock", "skipped (--skip-protection-checks)")
        probe.skip("docs_off", "skipped (--skip-protection-checks)")
    else:
        status, _, body = probe.request("GET", "/api/v1/meta", as_edge=False)
        probe.check(
            "origin_lock",
            status == 403 and is_error_envelope(body, "forbidden_origin"),
            f"status={status} (no {EDGE_AUTH_HEADER})",
        )
        status, _, _ = probe.request("GET", "/docs")
        probe.check("docs_off", status == 404, f"status={status}")

    print(f"smoke: {probe.failures} failed", flush=True)
    return 1 if probe.failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://api:10000")
    parser.add_argument("--fixture", action="store_true",
                        help="assert the fixture dataset's hand-computed estimate")
    parser.add_argument("--expect-build", default=None,
                        help="the ATUS_BUILD_SHA /health must report")
    parser.add_argument("--skip-protection-checks", action="store_true")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="per-request timeout in seconds (cold starts are slow)")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
