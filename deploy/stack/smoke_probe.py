"""Functional smoke checks against a running API (or the edge in front of it).

Standard library only, so it runs inside the API image on the compose network
(the API port is never published on the host). Prints one line per check and
exits non-zero if any check fails. Never prints the edge secret or the ops
token. Functional only: nothing here times anything.

    python smoke_probe.py --base-url http://api:10000 --fixture

Environment: ATUS_SMOKE_EDGE_SECRET is sent as ``X-ATUS-Edge-Auth`` on the
requests that model the edge (Worker -> API). Leave it unset when probing
through the edge, which adds the header itself. ATUS_SMOKE_OPS_TOKEN, when
set, is the API's ATUS_OPS_TOKEN: ``ops_gate`` then also expects /ready and
/metrics to answer 200 with it.

Checks:
  health            GET /health is 200 {"status": "ok", ...} (no edge secret; liveness
                    must answer the platform's health checker directly)
  build             /health reports the expected build (--expect-build)
  meta              GET /api/v1/meta through the edge path is 200 with api_version
  analysis          POST /api/v1/analysis/estimate is 200; with --fixture, the
                    pencil-and-paper value (sleep 2023 = 75.0 minutes)
  error_envelope    an unknown activity gets 422 unknown_activity in the v1 error envelope

Phase 5 API contract checks (docs/api.md; reported as SKIP with
``--skip-protection-checks``, which exists only for images built before the
API protection work landed):
  origin_lock       the same /meta request without the edge secret is 403
                    forbidden_origin (a direct hit on the onrender.com origin)
  docs_off          /docs is not served in production
  security_headers  CSP ``default-src 'none'; frame-ancestors 'none'``, nosniff,
                    ``Referrer-Policy: no-referrer``, CORP ``same-origin`` and HSTS
                    on a 200, a 403 and a 422; no ``Server`` header
  error_no_store    errors carry ``Cache-Control: no-store`` (never cached)
  analysis_headers  the analysis carries X-Response-Format 1, X-Analytics-Version,
                    X-Analysis-Key, X-Data-Version, an X-Cache verdict and
                    Server-Timing with db and app durations
  request_id        an edge-authenticated, well-formed X-Request-ID is echoed; the
                    same header without edge auth is replaced by the API's own
  body_limit        a body over 64 KiB is refused with 413 payload_too_large
  media_type        a POST that is not application/json gets 415 unsupported_media_type
  ops_gate          /ready and /metrics answer 404 not_found without the ops bearer
                    (and 200 with it when ATUS_SMOKE_OPS_TOKEN is set)
  data_version      X-Data-Version has a globally unique format (audit F06): the
                    loader's ``<release>:load-<run>-<UTC>-<hex6>`` or the serving
                    builder's ``<release>:serving-<UTC>-<hex8>:<hash12>``
"""

from __future__ import annotations

import argparse
import json
import os
import re
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

# The API's response contract (CONTRACT.md "Headers"; docs/api.md).
MAX_BODY_BYTES = 65536
SECURITY_HEADERS = {
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cross-origin-resource-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}
HSTS_PREFIX = "max-age=31536000"  # production only, which is what the stack runs
CACHE_VERDICTS = {"hit", "miss", "bypass", "join"}
RESPONSE_FORMAT = "1"
# CONTRACT.md "data_version": written by `atus load` or by the serving builder.
DATA_VERSION = re.compile(
    r"^[^:\s]+:(load-\d+-\d{8}T\d{6}Z-[0-9a-f]{6}|serving-\d{8}T\d{6}Z-[0-9a-f]{8}:[0-9a-f]{12})$"
)


class Probe:
    def __init__(self, base_url: str, edge_secret: str | None, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.edge_secret = edge_secret
        self.timeout = timeout
        self.failures = 0

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        as_edge: bool = True,
        raw_body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict, dict | None]:
        """Send one request; return (status, lower-cased headers, JSON body or None)."""
        sent = {"Accept": "application/json", "X-Request-ID": f"smoke-{uuid.uuid4().hex}"}
        data = raw_body
        if body is not None:
            data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
            sent["Content-Type"] = "application/json"
        if as_edge and self.edge_secret:
            sent[EDGE_AUTH_HEADER] = self.edge_secret
            sent[CLIENT_IP_HEADER] = SMOKE_CLIENT_IP
        sent.update(headers or {})
        req = urllib.request.Request(self.base_url + path, data=data, headers=sent, method=method)
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
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<17} {detail}", flush=True)
        if not ok:
            self.failures += 1

    @staticmethod
    def skip(name: str, detail: str) -> None:
        print(f"[SKIP] {name:<17} {detail}", flush=True)


def is_error_envelope(payload: dict | None, code: str | None = None) -> bool:
    """The v1 error shape: {"error": {"code", "message", "details"}}."""
    if not isinstance(payload, dict) or set(payload) != {"error"}:
        return False
    error = payload["error"]
    if not isinstance(error, dict) or not {"code", "message", "details"} <= set(error):
        return False
    return code is None or error["code"] == code


def error_code(payload: dict | None) -> str | None:
    error = payload.get("error") if isinstance(payload, dict) else None
    return error.get("code") if isinstance(error, dict) else None


def security_header_problems(headers: dict[str, str]) -> list[str]:
    """Contract security headers that are missing or wrong on one response."""
    problems = [
        f"{name}={headers.get(name)!r}"
        for name, expected in SECURITY_HEADERS.items()
        if headers.get(name) != expected
    ]
    if not headers.get("strict-transport-security", "").startswith(HSTS_PREFIX):
        problems.append(f"strict-transport-security={headers.get('strict-transport-security')!r}")
    if "server" in headers:
        problems.append(f"server={headers['server']!r}")
    return problems


def analysis_header_problems(headers: dict[str, str]) -> list[str]:
    """Contract headers missing or malformed on a successful analysis."""
    problems = []
    if headers.get("x-response-format") != RESPONSE_FORMAT:
        problems.append(f"x-response-format={headers.get('x-response-format')!r}")
    for name in ("x-analytics-version", "x-analysis-key", "x-data-version"):
        if not headers.get(name):
            problems.append(f"{name} missing")
    if headers.get("x-cache") not in CACHE_VERDICTS:
        problems.append(f"x-cache={headers.get('x-cache')!r}")
    timing = headers.get("server-timing", "")
    if "db;dur=" not in timing or "app;dur=" not in timing:
        problems.append(f"server-timing={timing!r}")
    return problems


def run_contract_checks(probe: Probe, responses: dict, ops_token: str | None) -> None:
    """The Phase 5 API contract, on the responses gathered above plus a few requests."""
    status, _, body = responses["no_edge"]
    probe.check(
        "origin_lock",
        status == 403 and is_error_envelope(body, "forbidden_origin"),
        f"status={status} (no {EDGE_AUTH_HEADER})",
    )
    status, _, _ = probe.request("GET", "/docs")
    probe.check("docs_off", status == 404, f"status={status}")

    problems = {
        f"{label} {responses[key][0]}": security_header_problems(responses[key][1])
        for label, key in (("meta", "meta"), ("direct", "no_edge"), ("invalid", "unknown"))
    }
    bad = {label: p for label, p in problems.items() if p}
    probe.check("security_headers", not bad,
                "; ".join(f"{label}: {', '.join(p)}" for label, p in bad.items())
                or "on " + ", ".join(problems) + "; no server header")

    no_store = {str(status): headers.get("cache-control")
                for status, headers, _ in (responses["no_edge"], responses["unknown"])
                if status >= 400}
    probe.check("error_no_store",
                bool(no_store) and all(v and "no-store" in v for v in no_store.values()),
                ", ".join(f"{label}: cache-control={v!r}" for label, v in no_store.items()))

    problems_list = analysis_header_problems(responses["analysis"][1])
    probe.check("analysis_headers", not problems_list,
                ", ".join(problems_list) or "format, versions, key, cache verdict, timing")

    request_id = f"smoke-echo-{uuid.uuid4().hex[:16]}"
    _, edge_headers, _ = probe.request("GET", "/api/v1/meta",
                                       headers={"X-Request-ID": request_id})
    _, direct_headers, _ = probe.request("GET", "/api/v1/meta", as_edge=False,
                                         headers={"X-Request-ID": request_id})
    echoed = edge_headers.get("x-request-id") == request_id
    replaced = direct_headers.get("x-request-id") not in (None, request_id)
    probe.check("request_id", echoed and replaced,
                f"edge-authenticated echoed={echoed}; direct replaced={replaced}")

    oversized = {**ESTIMATE_BODY, "padding": "x" * (MAX_BODY_BYTES + 1024)}
    status, _, body = probe.request("POST", "/api/v1/analysis/estimate", oversized)
    probe.check("body_limit", status == 413 and is_error_envelope(body, "payload_too_large"),
                f"status={status} code={error_code(body)!r}")

    status, _, body = probe.request(
        "POST", "/api/v1/analysis/estimate",
        raw_body=json.dumps(ESTIMATE_BODY).encode(), headers={"Content-Type": "text/plain"},
    )
    probe.check("media_type",
                status == 415 and is_error_envelope(body, "unsupported_media_type"),
                f"status={status} code={error_code(body)!r}")

    outcomes = []
    ok = True
    for path in ("/ready", "/metrics"):
        status, _, body = probe.request("GET", path)
        ok = ok and status == 404 and is_error_envelope(body, "not_found")
        outcomes.append(f"{path} without bearer={status}")
        if ops_token:
            status, _, _ = probe.request("GET", path,
                                         headers={"Authorization": f"Bearer {ops_token}"})
            ok = ok and status == 200
            outcomes.append(f"with bearer={status}")
    probe.check("ops_gate", ok, ", ".join(outcomes))

    version = responses["meta"][1].get("x-data-version")
    probe.check("data_version", bool(version and DATA_VERSION.match(version)),
                f"x-data-version={version!r}")


def run(args: argparse.Namespace) -> int:
    probe = Probe(args.base_url, os.environ.get("ATUS_SMOKE_EDGE_SECRET") or None, args.timeout)
    responses = {}

    status, _, body = probe.request("GET", "/health", as_edge=False)
    healthy = status == 200 and isinstance(body, dict) and body.get("status") == "ok"
    probe.check("health", healthy, f"status={status}")
    if args.expect_build:
        build = body.get("build") if isinstance(body, dict) else None
        probe.check("build", build == args.expect_build, f"build={build!r}")

    status, headers, body = responses["meta"] = probe.request("GET", "/api/v1/meta")
    api_version = body.get("api_version") if isinstance(body, dict) else None
    data_version = headers.get("x-data-version")
    probe.check(
        "meta",
        status == 200 and bool(api_version) and "x-request-id" in headers,
        f"status={status} api_version={api_version!r} data_version={data_version!r}",
    )

    status, headers, body = responses["analysis"] = probe.request(
        "POST", "/api/v1/analysis/estimate", ESTIMATE_BODY
    )
    value = (body or {}).get("estimate", {}).get("value") if isinstance(body, dict) else None
    ok = status == 200 and isinstance(value, (int, float))
    if args.fixture:
        ok = ok and abs(value - FIXTURE_SLEEP_2023_MINUTES) < 1e-9
    probe.check(
        "analysis", ok, f"status={status} value={value!r} x-cache={headers.get('x-cache')!r}"
    )

    status, headers, body = responses["unknown"] = probe.request(
        "POST", "/api/v1/analysis/estimate", UNKNOWN_ACTIVITY_BODY
    )
    probe.check(
        "error_envelope",
        status == 422 and is_error_envelope(body, "unknown_activity"),
        f"status={status} code={error_code(body)!r}",
    )

    if args.skip_protection_checks:
        for name in ("origin_lock", "docs_off", "security_headers", "error_no_store",
                     "analysis_headers", "request_id", "body_limit", "media_type", "ops_gate",
                     "data_version"):
            probe.skip(name, "skipped (--skip-protection-checks)")
    else:
        responses["no_edge"] = probe.request("GET", "/api/v1/meta", as_edge=False)
        run_contract_checks(probe, responses, os.environ.get("ATUS_SMOKE_OPS_TOKEN") or None)

    print(f"smoke: {probe.failures} failed", flush=True)
    return 1 if probe.failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://api:10000")
    parser.add_argument("--fixture", action="store_true",
                        help="assert the fixture dataset's hand-computed estimate")
    parser.add_argument("--expect-build", default=None,
                        help="the ATUS_BUILD_SHA /health must report")
    parser.add_argument("--skip-protection-checks", action="store_true",
                        help="report the Phase 5 API contract checks as SKIP (old images)")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="per-request timeout in seconds (cold starts are slow)")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
