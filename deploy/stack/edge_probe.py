"""End-to-end checks through the local stack's edge (the public origin), stdlib only.

Run by ``scripts/smoke_stack.sh --edge`` from the host against the `edge`
service of docker-compose.prod.yml: the Worker from edge/ under
``wrangler dev`` (local mode) serving the SPA build and the static snapshot,
in front of the production-mode API. Each check prints one PASS/FAIL line:

  spa_fallback      deep links (/explore, /about, /analysis/..., unknown paths)
                    get the SPA's index.html with 200 (Workers SPA fallback)
  static_headers    _headers on static responses (/, /explore, a hashed
                    asset): every header of its `/*` rule with exactly the
                    value the file gives (CSP with connect-src 'self', HSTS,
                    nosniff, Referrer-Policy, Permissions-Policy, COOP);
                    index.html no-cache, hashed assets immutable
  snapshot_static   /snap/current.json (no-cache) points at a manifest whose
                    files are served byte for byte as built, as static files
                    (no Worker header), immutable
  snapshot_matches  the snapshot's /meta is byte-identical to the API's own
                    /api/v1/meta (asked with a query string: the Worker answers
                    the plain path from the snapshot itself)
  edge_health       /api/_edge/health reports the release vars and a cache scope
  api_allowlist     unknown /api paths, /ready, /docs, /openapi.json, /metrics,
                    trailing slashes and case variants: 404 envelopes; wrong
                    methods 405; nothing reaches the origin
  api_headers       the Worker's security headers on /api responses (200 and
                    errors), no-store, no `server` or `x-render-*` header
  limits            413 over 64 KiB and 415 for a non-JSON analysis, at the edge
  kv_cache          an analysis computed once (X-Cache: miss) is then answered
                    by the edge (X-Cache: edge), byte for byte
  governor_denial   one client network's 61st new analysis of the day is refused
                    by the governor: 429 rate_limited, scope client, budget
                    prefix, Retry-After until 00:00 UTC
  ops_budget        /api/_ops/budget is 404 without the bearer, 200 with it
  wake              /api/v1/wake answers {origin: ...} without an origin error
  origin_lock       (with --direct-api) the API itself, without the edge
                    secret: 403 forbidden_origin, forged forwarding headers too

Client networks: ``wrangler dev`` sets ``CF-Connecting-IP`` from the socket
only when the request has none (Miniflare), so the probe sends its own from
198.18.0.0/15 (benchmarking addresses, never routable) to act as distinct
visitors; Cloudflare's edge overwrites that header in production. The
governor check spends one /24's daily allowance, from addresses no browser
uses.

    python deploy/stack/edge_probe.py --base http://127.0.0.1:8541 \\
        --assets /path/to/assets [--direct-api http://127.0.0.1:NNNN]

The Worker's ops bearer comes from ATUS_PROBE_EDGE_OPS_TOKEN and the
expected data version from ATUS_PROBE_DATA_VERSION (both optional).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

ESTIMATE = "/api/v1/analysis/estimate"
HEADERS_FILE = Path(__file__).resolve().parents[2] / "frontend" / "public" / "_headers"
#: Headers the `/*` rule of _headers must set (brief §11); the probe checks
#: every header of that rule, these at least.
REQUIRED_SITE_HEADERS = ("content-security-policy", "strict-transport-security",
                         "x-content-type-options", "referrer-policy", "permissions-policy")
NAVIGATION = {"Accept": "text/html", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
API_SECURITY = {
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cross-origin-resource-policy": "same-origin",
    "cache-control": "no-store",
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self):
        return json.loads(self.body)

    def code(self) -> str | None:
        try:
            return self.json()["error"]["code"]
        except (ValueError, KeyError, TypeError):
            return None


def request(base: str, path: str, *, method: str = "GET", body: bytes | None = None,
            headers: dict[str, str] | None = None) -> Reply:
    req = urllib.request.Request(base + path, data=body, method=method,
                                 headers={"Accept-Encoding": "identity", **(headers or {})})
    try:
        with _OPENER.open(req, timeout=120) as response:
            return Reply(response.status, {k.lower(): v for k, v in response.headers.items()},
                         response.read())
    except urllib.error.HTTPError as exc:
        return Reply(exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read())


def visitor(network: int, host: int) -> dict[str, str]:
    """A client address in 198.18.0.0/15: network picks the /24, host the address."""
    return {"CF-Connecting-IP": f"198.{18 + network // 256}.{network % 256}.{host}"}


def analysis_body(label: str) -> bytes:
    return json.dumps({"activity": {"include": ["010101"], "label": label}, "years": [2023]},
                      separators=(",", ":"), sort_keys=True).encode()


def site_headers(path: Path = HEADERS_FILE) -> dict[str, str]:
    """The headers the `/*` rule of a `_headers` file sets on every static
    path (lowercase names, values as written)."""
    rules: dict[str, dict[str, str]] = {}
    current: str | None = None
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            current = line.strip()
            rules.setdefault(current, {})
        elif current is not None:
            name, _, value = line.strip().partition(":")
            rules[current][name.strip().lower()] = value.strip()
    expected = rules.get("/*", {})
    missing = [name for name in REQUIRED_SITE_HEADERS if not expected.get(name)]
    if missing:
        raise SystemExit(f"{path}: the /* rule does not set {', '.join(missing)}")
    return expected


@dataclass
class Probe:
    base: str
    results: list[tuple[str, bool, str]] = field(default_factory=list)
    site: dict[str, str] = field(default_factory=site_headers)

    def check(self, name: str, ok: bool, detail: str) -> bool:
        self.results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<17} {detail}", flush=True)
        return ok

    def get(self, path: str, **kwargs) -> Reply:
        return request(self.base, path, **kwargs)


def check_spa_fallback(probe: Probe) -> None:
    bad = []
    for path in ("/", "/explore", "/about", "/analysis/estimate?spec=not-a-spec",
                 "/analysis/trend", "/no/such/page"):
        reply = probe.get(path, headers=NAVIGATION)
        if not (reply.status == 200 and reply.headers.get("content-type", "").startswith(
                "text/html") and b'<div id="root"' in reply.body):
            bad.append(f"{path}={reply.status}")
    probe.check("spa_fallback", not bad, "deep links -> index.html 200" if not bad else str(bad))


def _static_security_problems(headers: dict[str, str], expected: dict[str, str]) -> list[str]:
    """Each header of `expected` (the `/*` rule) missing or with another value."""
    problems = [f"{name}={headers.get(name)!r}" for name, value in expected.items()
                if headers.get(name) != value]
    csp = headers.get("content-security-policy", "")
    if "connect-src 'self'" not in csp or "frame-ancestors 'none'" not in csp:
        problems.append("csp")
    if "x-request-id" in headers:
        problems.append("worker-generated")
    return problems


def check_static_headers(probe: Probe) -> str | None:
    index = probe.get("/", headers=NAVIGATION)
    problems = [f"/:{p}" for p in _static_security_problems(index.headers, probe.site)]
    if "no-cache" not in index.headers.get("cache-control", ""):
        problems.append("/:cache-control")
    deep = probe.get("/explore", headers=NAVIGATION)
    problems += [f"/explore:{p}" for p in _static_security_problems(deep.headers, probe.site)]
    match = re.search(rb'src="(/assets/[^"]+\.js)"', index.body)
    asset = match.group(1).decode() if match else None
    if asset is None:
        problems.append("no /assets/*.js in index.html")
    else:
        reply = probe.get(asset)
        if reply.status != 200 or "immutable" not in reply.headers.get("cache-control", ""):
            problems.append(f"{asset}:{reply.status}/{reply.headers.get('cache-control')}")
        problems += [f"{asset}:{p}" for p in _static_security_problems(reply.headers,
                                                                      probe.site)]
    probe.check("static_headers", not problems,
                f"the {len(probe.site)} headers of _headers' /* rule, exact (CSP connect-src "
                "'self', HSTS, nosniff, Referrer/Permissions-Policy); index no-cache, assets "
                "immutable" if not problems else str(problems))
    return asset


def check_snapshot(probe: Probe, assets: Path | None) -> dict | None:
    pointer_reply = probe.get("/snap/current.json")
    try:
        pointer = pointer_reply.json()
        sid = pointer["snapshot_id"]
    except (ValueError, KeyError, TypeError):
        probe.check("snapshot_static", False, f"/snap/current.json: {pointer_reply.status}")
        return None
    problems = []
    if "no-cache" not in pointer_reply.headers.get("cache-control", ""):
        problems.append("current.json not no-cache")
    manifest_reply = probe.get(f"/snap/{sid}/manifest.json")
    manifest = manifest_reply.json()
    if "immutable" not in manifest_reply.headers.get("cache-control", ""):
        problems.append("manifest not immutable")
    files = list(manifest["files"].items())
    sample = [f for f in files if f[0].startswith("post/")][:5] + files[:10]
    for name, entry in sample:
        reply = probe.get(f"/snap/{sid}/{name}")
        if reply.status != 200 or len(reply.body) != entry["bytes"]:
            problems.append(f"{name}:{reply.status}")
        elif "x-request-id" in reply.headers:
            problems.append(f"{name}: answered by the Worker")
        elif assets is not None and reply.body != (assets / "snap" / sid / name).read_bytes():
            problems.append(f"{name}: bytes differ from the build")
    posts = len(manifest.get("post", {}))
    probe.check("snapshot_static", not problems,
                f"{sid}: {len(files)} files, {posts} prewarmed analyses, sampled "
                f"{len(sample)} served as built" if not problems else str(problems))
    return {"snapshot_id": sid, **pointer}


def check_snapshot_matches_api(probe: Probe, snapshot: dict | None) -> None:
    if snapshot is None:
        probe.check("snapshot_matches", False, "no snapshot")
        return
    sid = snapshot["snapshot_id"]
    static = probe.get(f"/snap/{sid}/get/meta.json")
    # The Worker answers a plain /api/v1/meta from this same snapshot file; a
    # query string (which the API ignores) makes it ask the API instead.
    live = probe.get("/api/v1/meta?probe=snapshot_matches", headers=visitor(0, 10))
    served = probe.get("/api/v1/meta", headers=visitor(0, 10))
    same = static.body == live.body == served.body
    version = live.headers.get("x-data-version")
    probe.check("snapshot_matches",
                same and version == snapshot.get("data_version")
                and served.headers.get("x-data-version") == version,
                f"/meta static == API /api/v1/meta == edge /api/v1/meta ({len(live.body)} B), "
                f"data_version {version}")


def check_edge_health(probe: Probe, expected_version: str | None,
                      snapshot: dict | None) -> None:
    reply = probe.get("/api/_edge/health")
    body = reply.json() if reply.status == 200 else {}
    ok = (reply.status == 200 and body.get("status") == "ok"
          and body.get("cache_scope") is not None
          and (expected_version is None or body.get("data_version") == expected_version)
          and (snapshot is None or body.get("snapshot_id") == snapshot["snapshot_id"]))
    probe.check("edge_health", ok, f"status={reply.status} data_version="
                f"{body.get('data_version')!r} snapshot_id={body.get('snapshot_id')!r} "
                f"cache_scope={'set' if body.get('cache_scope') else None}")


def check_allowlist(probe: Probe) -> None:
    wrong = []
    for path in ("/api", "/api/", "/api/docs", "/api/redoc", "/api/openapi.json",
                 "/api/ready", "/api/metrics", "/api/v1/meta/", "/api/V1/meta",
                 "/api/v1/nope", "/api/v1/activities/%2e%2e", "/api/_ops/budget"):
        reply = probe.get(path)
        if reply.status != 404 or reply.code() != "not_found":
            wrong.append(f"GET {path}={reply.status}/{reply.code()}")
    for method, path, expected in (("POST", "/api/v1/meta", 405),
                                   ("GET", ESTIMATE, 405),
                                   ("DELETE", "/api/v1/activities", 405)):
        reply = probe.get(path, method=method, body=b"{}" if method == "POST" else None,
                          headers={"Content-Type": "application/json"})
        if reply.status != expected or reply.code() != "method_not_allowed":
            wrong.append(f"{method} {path}={reply.status}")
    ok = probe.get("/api/v1/meta", headers=visitor(0, 11))
    if ok.status != 200 or not ok.headers.get("x-data-version"):
        wrong.append(f"GET /api/v1/meta={ok.status}")
    probe.check("api_allowlist", not wrong,
                "only the allowlisted routes reach the API" if not wrong else str(wrong))


def check_api_headers(probe: Probe) -> None:
    problems = []
    for label, reply in (("meta 200", probe.get("/api/v1/meta", headers=visitor(0, 12))),
                         ("404", probe.get("/api/nope")),
                         ("422", probe.get(ESTIMATE, method="POST", body=b'{"years":[]}',
                                           headers={"Content-Type": "application/json",
                                                    **visitor(0, 12)}))):
        for name, value in API_SECURITY.items():
            if reply.headers.get(name) != value:
                problems.append(f"{label}:{name}={reply.headers.get(name)!r}")
        if "max-age=" not in reply.headers.get("strict-transport-security", ""):
            problems.append(f"{label}:hsts")
        if "permissions-policy" not in reply.headers:
            problems.append(f"{label}:permissions-policy")
        leaked = [h for h in reply.headers if h == "server" or h.startswith("x-render")
                  or h == "rndr-id"]
        if leaked:
            problems.append(f"{label}:{leaked}")
        if reply.headers.get("content-type") != "application/json":
            problems.append(f"{label}:content-type={reply.headers.get('content-type')}")
    probe.check("api_headers", not problems,
                "CSP default-src 'none', HSTS, nosniff, no-referrer, CORP, "
                "Permissions-Policy, no-store; no server header" if not problems
                else str(problems))


def check_limits(probe: Probe) -> None:
    big = probe.get(ESTIMATE, method="POST", body=b" " * (64 * 1024 + 1),
                    headers={"Content-Type": "application/json", **visitor(0, 13)})
    text = probe.get(ESTIMATE, method="POST", body=b"{}",
                     headers={"Content-Type": "text/plain", **visitor(0, 13)})
    probe.check("limits", (big.status, big.code(), text.status, text.code())
                == (413, "payload_too_large", 415, "unsupported_media_type"),
                f"64 KiB+1: {big.status} {big.code()}, text/plain: {text.status} {text.code()}")


def check_kv_cache(probe: Probe) -> None:
    body = analysis_body(f"edge-probe {secrets.token_hex(6)}")
    headers = {"Content-Type": "application/json", **visitor(1, 1)}
    first = probe.get(ESTIMATE, method="POST", body=body, headers=headers)
    second = first
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        time.sleep(0.2)  # the store runs through waitUntil, after the first answer
        second = probe.get(ESTIMATE, method="POST", body=body, headers=headers)
        if second.headers.get("x-cache") == "edge":
            break
    ok = (first.status == 200 and first.headers.get("x-cache") == "miss"
          and second.status == 200 and second.headers.get("x-cache") == "edge"
          and second.body == first.body
          and second.headers.get("x-data-version") == first.headers.get("x-data-version"))
    probe.check("kv_cache", ok, f"first {first.status} X-Cache={first.headers.get('x-cache')}"
                f", then {second.status} X-Cache={second.headers.get('x-cache')}, "
                f"bytes {'identical' if second.body == first.body else 'DIFFER'}")


def forged_address(network: int) -> dict[str, str]:
    """Forwarding headers claiming another /24 (security.md abuse case 5)."""
    address = visitor(network, 1)["CF-Connecting-IP"]
    return {"X-Forwarded-For": address, "X-Real-IP": address, "True-Client-IP": address,
            "Forwarded": f"for={address}"}


def check_governor(probe: Probe) -> None:
    """61 new analyses from one /24 (distinct addresses, so the API's own
    per-address buckets never shed them): the 61st is the governor's. Each
    also claims a different /24 in its forwarding headers, which must not
    change the network the governor charges (review r3)."""
    run = secrets.token_hex(4)
    statuses: list[int] = []
    denial: Reply | None = None
    for host in range(1, 62):
        reply = probe.get(ESTIMATE, method="POST", body=analysis_body(f"gov {run} {host}"),
                          headers={"Content-Type": "application/json", **visitor(2, host),
                                   # 198.18.101.1 to .161.1: the probe's own /16
                                   **forged_address(100 + host)})
        statuses.append(reply.status)
        if reply.status != 200:
            denial = reply
            break
    details = (denial.json().get("error", {}).get("details") or {}) if denial else {}
    retry_after = int(denial.headers.get("retry-after", "0")) if denial else 0
    ok = (len(statuses) == 61 and statuses[:60] == [200] * 60 and denial is not None
          and denial.status == 429 and denial.code() == "rate_limited"
          and details.get("scope") == "client" and details.get("budget") == "prefix"
          and 0 < retry_after <= 86400 and _midnight_utc(details.get("resumes_at")))
    probe.check("governor_denial", ok,
                f"{statuses.count(200)} admitted, then {denial.status if denial else '-'} "
                f"{denial.code() if denial else '-'} scope={details.get('scope')} "
                f"budget={details.get('budget')} Retry-After={retry_after} "
                f"resumes_at={details.get('resumes_at')}")


def _midnight_utc(value: object) -> bool:
    """An ISO-8601 time at 00:00 UTC (the governor resets every UTC day)."""
    if not isinstance(value, str):
        return False
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return moment.utcoffset() == timedelta(0) and moment.time() == dt_time(0, 0)


def check_ops_budget(probe: Probe, token: str | None) -> None:
    anonymous = probe.get("/api/_ops/budget")
    wrong = probe.get("/api/_ops/budget", headers={"Authorization": "Bearer nope"})
    ok = anonymous.status == 404 and wrong.status == 404
    detail = f"no bearer {anonymous.status}, wrong {wrong.status}"
    if token:
        bearer = probe.get("/api/_ops/budget", headers={"Authorization": f"Bearer {token}"})
        ok = ok and bearer.status == 200 and "governor" in bearer.json()
        detail += f", bearer {bearer.status}"
    probe.check("ops_budget", ok, detail)


def check_wake(probe: Probe) -> None:
    reply = probe.get("/api/v1/wake", headers=visitor(0, 14))
    origin = reply.json().get("origin") if reply.status == 200 else None
    probe.check("wake", reply.status == 200 and origin in ("warm", "waking", "unknown"),
                f"status={reply.status} origin={origin!r}")


def check_origin_lock(probe: Probe, direct: str) -> None:
    plain = request(direct, "/api/v1/meta")
    forged = request(direct, "/api/v1/meta", headers={
        "X-ATUS-Client-IP": "198.51.100.1", "X-Forwarded-For": "198.51.100.1",
        "CF-Connecting-IP": "198.51.100.1", "X-ATUS-Edge-Auth": "not-the-secret"})
    ok = all(r.status == 403 and r.code() == "forbidden_origin" for r in (plain, forged))
    probe.check("origin_lock", ok, f"direct without the secret: {plain.status} "
                f"{plain.code()}; forged headers: {forged.status} {forged.code()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="the edge, e.g. http://127.0.0.1:8541")
    parser.add_argument("--assets", type=Path, help="the published assets (SPA + snap/)")
    parser.add_argument("--direct-api", help="the API itself, for the origin-lock check")
    parser.add_argument("--skip-governor", action="store_true",
                        help="do not spend a /24's daily allowance")
    args = parser.parse_args(argv)
    # The published copy of _headers when the assets are given, else the source.
    published = args.assets / "_headers" if args.assets else None
    probe = Probe(args.base.rstrip("/"), site=site_headers(
        published if published is not None and published.is_file() else HEADERS_FILE))
    expected_version = os.environ.get("ATUS_PROBE_DATA_VERSION") or None

    check_spa_fallback(probe)
    check_static_headers(probe)
    snapshot = check_snapshot(probe, args.assets)
    check_snapshot_matches_api(probe, snapshot)
    check_edge_health(probe, expected_version, snapshot)
    check_allowlist(probe)
    check_api_headers(probe)
    check_limits(probe)
    check_kv_cache(probe)
    check_wake(probe)
    check_ops_budget(probe, os.environ.get("ATUS_PROBE_EDGE_OPS_TOKEN") or None)
    if args.direct_api:
        check_origin_lock(probe, args.direct_api.rstrip("/"))
    if not args.skip_governor:
        check_governor(probe)

    failed = sum(1 for _, ok, _ in probe.results if not ok)
    print(f"edge probe: {failed} failed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
