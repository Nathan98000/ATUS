#!/usr/bin/env python3
"""Post-deploy smoke test of the public site, at most 40 requests (ADR-011 "Deploy").

    python scripts/ops/deploy_smoke.py --base-url https://atus.<sub>.workers.dev \\
        --expect-build "$GITHUB_SHA" --expect-data-version "<data version>" \\
        --expect-snapshot-id "<snapshot id>" --require-snapshot --recompute 2 \\
        [--origin-lock] [--ops-budget] [--report smoke.json]

Checks, in order (brief §16, §17; deployment.md "8. Prove the deployment"):

  shell            GET /: 200 text/html with the site's security headers and
                   ``Cache-Control: no-cache`` (frontend/public/_headers)
  deep_link        GET /analysis/estimate: the SPA fallback, with the <meta>
                   CSP backstop (a missing CSP header there is a warning:
                   ADR-011 lists "_headers on the SPA fallback" as unverified)
  snapshot         /snap/current.json and its manifest: the expected snapshot
                   id and data version (static files, no Worker)
  edge_health      GET /api/_edge/health: the Worker's vars match the deploy
  api_health       GET /api/health through the Worker to Render's /health,
                   with a cold-start budget; reports the expected build
  meta             GET /api/v1/meta: v1, and X-Data-Version as expected
  starter_<n>      a home-page starter analysis served from the static
                   snapshot, recomputed live through the Worker with different
                   request bytes (so the edge cache cannot answer it) and
                   diffed under the §9 equivalence criterion
  api_errors       an unknown /api path: the v1 404 envelope and the Worker's
                   security headers
  origin_lock      (--origin-lock, RENDER_ORIGIN) the onrender.com origin
                   refuses a request without the edge secret: 403 forbidden_origin
  ops_budget       (--ops-budget, EDGE_OPS_TOKEN) the governor's budget report

Without a deployed snapshot (the local production stack in CI) the starter is
computed live and only its shape is checked. Secrets come from the
environment and are never printed. The §9 comparison needs the atus_pipeline
package (``pip install -e .``). Exit status: 0 pass, 1 fail, 2 usage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from httpcheck import (  # noqa: E402
    API_HEADERS,
    SITE_HEADERS,
    BudgetExceeded,
    Client,
    OpsError,
    Report,
    header_problems,
)

HARD_MAX_REQUESTS = 40
SNAPSHOT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ANALYSIS_STATUSES = ("miss", "hit", "join", "bypass", "edge")

#: The home page's first and third starters (frontend/src/domain/prewarm.ts,
#: STARTER_EXAMPLES; tests/unit/ops/test_deploy_smoke.py keeps them in step).
STARTERS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("sleep", "estimate", {"measure": "average_minutes_per_day",
                           "activity": {"preset": "sleep"}}),
    ("tv_by_sex", "compare", {"measure": "average_minutes_per_day",
                              "activity": {"preset": "watching_tv"},
                              "group_a": {"sex": "male"}, "group_b": {"sex": "female"},
                              "label_a": "Men", "label_b": "Women"}),
)


def stable_stringify(value: Any) -> str:
    """The SPA's ``stableStringify``: sorted keys, no whitespace (snapshot keys)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_key(path: str, body: str) -> str:
    """``sha256_hex(path + "?" + query + "\\n" + body)`` with an empty query."""
    return hashlib.sha256(f"{path}?\n{body}".encode()).hexdigest()


def compare_under_criterion(a: Any, b: Any) -> tuple[bool, str]:
    """The §9 criterion (``atus_pipeline.serving.equivalence``)."""
    try:
        from atus_pipeline.serving.equivalence import compare_results
    except ImportError as exc:  # pragma: no cover - environment problem
        raise OpsError("the §9 comparison needs the atus_pipeline package installed") from exc
    result = compare_results(a, b)
    if result.equivalent:
        return True, (f"{result.compared} values equivalent; largest scaled SE/CI "
                      f"difference {result.max_scaled:.3g}")
    return False, "; ".join(result.mismatches[:3])


class Smoke:
    def __init__(self, client: Client, report: Report, args: argparse.Namespace) -> None:
        self.client = client
        self.report = report
        self.args = args
        self.snapshot_id: str | None = None
        self.manifest: dict | None = None
        self.latest_year: int | None = None

    # -- static site --------------------------------------------------------
    def shell(self) -> None:
        reply = self.client.get("/")
        problems = header_problems(reply, SITE_HEADERS)
        cache = (reply.header("cache-control") or "").lower()
        if reply.status != 200 or reply.content_type != "text/html":
            self.report.fail("shell", f"expected 200 text/html, got {reply.describe()}")
        elif problems:
            self.report.fail("shell", "security headers: " + ", ".join(problems))
        elif "no-cache" not in cache:
            self.report.fail("shell", "index.html is not served with Cache-Control: no-cache")
        else:
            self.report.ok("shell", "SPA shell with CSP, HSTS, nosniff, Referrer-Policy, "
                                    "Permissions-Policy and no-cache")

    def deep_link(self) -> None:
        reply = self.client.get("/analysis/estimate")
        meta_csp = b'http-equiv="Content-Security-Policy"' in reply.body
        if reply.status != 200 or reply.content_type != "text/html":
            self.report.fail("deep_link", f"expected the SPA fallback, got {reply.describe()}")
        elif not meta_csp:
            self.report.fail("deep_link", "the SPA fallback has no <meta> CSP backstop")
        elif reply.header("content-security-policy") is None:
            self.report.add("deep_link", "WARN", "SPA fallback served without the CSP header "
                            "(_headers did not apply); the <meta> CSP backstop is present")
        else:
            self.report.ok("deep_link", "SPA fallback (200) with the CSP header and backstop")

    def snapshot(self) -> None:
        pointer = self.client.get("/snap/current.json")
        if pointer.status != 200 or not pointer.is_json:
            if self.args.require_snapshot:
                self.report.fail("snapshot", "no static snapshot is served "
                                 f"({pointer.describe()} {pointer.content_type})")
            else:
                self.report.add("snapshot", "SKIP", "no snapshot deployed (the SPA reads "
                                "everything from the API)")
            return
        body = pointer.json()
        snapshot_id = body.get("snapshot_id")
        if not isinstance(snapshot_id, str) or not SNAPSHOT_ID.match(snapshot_id):
            self.report.fail("snapshot", "the pointer names no valid snapshot_id")
            return
        expected = self.args.expect_snapshot_id
        if expected and snapshot_id != expected:
            self.report.fail("snapshot", f"serves {snapshot_id}, expected {expected}")
            return
        dv = self.args.expect_data_version
        if dv and body.get("data_version") != dv:
            self.report.fail("snapshot", f"data version {body.get('data_version')}, expected {dv}")
            return
        manifest = self.client.get(f"/snap/{snapshot_id}/manifest.json")
        if manifest.status != 200 or not manifest.is_json:
            self.report.fail("snapshot", f"manifest: {manifest.describe()}")
            return
        self.manifest = manifest.json()
        meta_file = (self.manifest.get("get") or {}).get("/api/v1/meta")
        if isinstance(meta_file, str):
            meta = self.client.get(f"/snap/{snapshot_id}/{meta_file}")
            if meta.status == 200 and meta.is_json:
                years = ((meta.json().get("data") or {}).get("years")) or []
                self.latest_year = max(years) if years else None
        self.snapshot_id = snapshot_id
        self.report.ok("snapshot", f"{snapshot_id}: pointer, manifest "
                       f"({len(self.manifest.get('post') or {})} analyses) and metadata served")

    # -- through the Worker --------------------------------------------------
    def edge_health(self) -> None:
        reply = self.client.get("/api/_edge/health")
        if reply.status != 200 or not reply.is_json:
            self.report.fail("edge_health", f"expected 200 JSON, got {reply.describe()}")
            return
        body = reply.json()
        problems = header_problems(reply, API_HEADERS)
        wrong = []
        if self.args.expect_data_version and body.get("data_version") != \
                self.args.expect_data_version:
            wrong.append(f"DATA_VERSION {body.get('data_version')}")
        if self.args.expect_snapshot_id and body.get("snapshot_id") != \
                self.args.expect_snapshot_id:
            wrong.append(f"SNAPSHOT_ID {body.get('snapshot_id')}")
        if problems:
            self.report.fail("edge_health", "security headers: " + ", ".join(problems))
        elif wrong:
            self.report.fail("edge_health", "the Worker runs other vars: " + ", ".join(wrong))
        else:
            self.report.ok("edge_health", f"edge {body.get('edge_version')}, cache scope "
                                          f"{body.get('cache_scope')}")

    def api_health(self) -> None:
        def ok(reply) -> bool:
            if reply.status != 200 or not reply.is_json:
                return False
            try:
                return reply.json().get("status") == "ok"
            except OpsError:
                return False

        try:
            reply = self.client.wait_for(
                "GET", "/api/health", ok, deadline_s=self.args.wake_timeout,
                min_interval_s=15.0, max_attempts=8, timeout=120.0)
        except OpsError as exc:
            self.report.fail("api_health", str(exc))
            return
        build = reply.json().get("build")
        if self.args.expect_build and build != self.args.expect_build:
            self.report.fail("api_health", f"Render runs build {build}, expected "
                                           f"{self.args.expect_build}")
        else:
            self.report.ok("api_health", f"Render /health through the Worker (build {build})")

    def meta(self) -> None:
        reply = self.client.get("/api/v1/meta")
        if reply.status != 200 or not reply.is_json:
            self.report.fail("meta", f"expected 200 JSON, got {reply.describe()}")
            return
        body = reply.json()
        version = reply.header("x-data-version")
        expected = self.args.expect_data_version
        if body.get("api_version") != "v1":
            self.report.fail("meta", f"api_version {body.get('api_version')!r}")
        elif expected and version != expected:
            self.report.fail("meta", f"X-Data-Version {version}, expected {expected}")
        else:
            years = ((body.get("data") or {}).get("years")) or []
            if self.latest_year is None and years:
                self.latest_year = max(years)
            self.report.ok("meta", f"v1, data version {version}, "
                                   f"analytics {body.get('analytics_version')}")

    def starters(self) -> None:
        if self.latest_year is None:
            self.report.fail("starters", "no years known (/meta failed)")
            return
        for index, (name, operation, spec) in enumerate(STARTERS[: self.args.recompute], 1):
            self.starter(index, name, operation, {**spec, "years": [self.latest_year]})

    def starter(self, index: int, name: str, operation: str, spec: dict) -> None:
        check = f"starter_{index}_{name}"
        path = f"/api/v1/analysis/{operation}"
        static = None
        if self.manifest is not None:
            key = request_key(path, stable_stringify(spec))
            entry = (self.manifest.get("post") or {}).get(key)
            if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
                self.report.fail(check, "the starter is not in the snapshot manifest (the "
                                        "SPA would send it to the API)")
                return
            reply = self.client.get(f"/snap/{self.snapshot_id}/{entry['file']}")
            if reply.status != 200 or not reply.is_json:
                self.report.fail(check, f"snapshot file: {reply.describe()}")
                return
            static = reply.json()
        # Different bytes than the SPA sends, so the edge cache cannot answer it.
        live = self.client.post_json(path, json.dumps(spec, indent=1).encode(), timeout=150.0)
        if live.status != 200 or not live.is_json:
            self.report.fail(check, f"live analysis: {live.describe()}")
            return
        x_cache = live.header("x-cache")
        dv = self.args.expect_data_version
        if dv and live.header("x-data-version") != dv:
            self.report.fail(check, f"live X-Data-Version {live.header('x-data-version')}")
            return
        if static is None:
            payload = live.json()
            shape_ok = "estimate" in payload or "difference" in payload
            if shape_ok:
                self.report.ok(check, f"computed live (X-Cache {x_cache}); no snapshot to diff")
            else:
                self.report.fail(check, "the live response is not an analysis result")
            return
        equivalent, detail = compare_under_criterion(static, live.json())
        if not equivalent:
            self.report.fail(check, f"snapshot and live results differ under §9: {detail}")
        elif x_cache == "edge":
            self.report.add(check, "WARN", f"equivalent, but the live call was an edge-cache "
                                           f"hit, not a recompute ({detail})")
        else:
            self.report.ok(check, f"snapshot vs live (X-Cache {x_cache}): {detail}")

    def api_errors(self) -> None:
        reply = self.client.get("/api/v1/no-such-route")
        problems = header_problems(reply, API_HEADERS)
        if reply.status != 404 or reply.error_code != "not_found":
            self.report.fail("api_errors", f"expected the 404 not_found envelope, got "
                                           f"{reply.describe()}")
        elif problems:
            self.report.fail("api_errors", "security headers: " + ", ".join(problems))
        else:
            self.report.ok("api_errors", "404 envelope with the Worker's security headers")

    # -- optional, secret-backed ---------------------------------------------
    def origin_lock(self) -> None:
        origin_url = os.environ.get("RENDER_ORIGIN", "").strip()
        if not origin_url:
            self.report.fail("origin_lock", "--origin-lock needs RENDER_ORIGIN")
            return
        origin = Client(origin_url, label="origin", max_requests=1, timeout=120.0)
        if self.client.remaining <= 0:
            raise BudgetExceeded("request budget spent; not checking the origin lock")
        self.client.sent += 1  # one request of this smoke's budget, sent to the origin
        reply = origin.get("/api/v1/meta")
        if reply.status == 403 and reply.error_code == "forbidden_origin":
            self.report.ok("origin_lock", "the origin refuses requests without the edge secret")
        else:
            self.report.fail("origin_lock", f"expected 403 forbidden_origin, got "
                                            f"{reply.describe()}")

    def ops_budget(self) -> None:
        token = os.environ.get("EDGE_OPS_TOKEN", "").strip()
        if not token:
            self.report.fail("ops_budget", "--ops-budget needs EDGE_OPS_TOKEN")
            return
        reply = self.client.get("/api/_ops/budget", headers={"Authorization": f"Bearer {token}"})
        if reply.status != 200 or not reply.is_json:
            self.report.fail("ops_budget", f"expected 200 JSON, got {reply.describe()}")
            return
        governor = reply.json().get("governor") or {}
        self.report.ok("ops_budget", f"governor in {governor.get('mode')} mode; "
                                     f"{(governor.get('analyses') or {}).get('admitted')} "
                                     "uncached analyses admitted today")

    def run(self) -> None:
        steps = [self.shell, self.deep_link, self.snapshot, self.edge_health, self.api_health,
                 self.meta, self.starters, self.api_errors]
        if self.args.origin_lock:
            steps.append(self.origin_lock)
        if self.args.ops_budget:
            steps.append(self.ops_budget)
        for step in steps:
            try:
                step()
            except OpsError as exc:
                self.report.fail(step.__name__, str(exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default=os.environ.get("PUBLIC_BASE_URL"))
    parser.add_argument("--expect-build", default=None)
    parser.add_argument("--expect-data-version", default=None)
    parser.add_argument("--expect-snapshot-id", default=None)
    parser.add_argument("--require-snapshot", action="store_true")
    parser.add_argument("--recompute", type=int, choices=(1, 2), default=1,
                        help="starters to diff against the snapshot (default 1)")
    parser.add_argument("--max-requests", type=int, default=30)
    parser.add_argument("--wake-timeout", type=float, default=240.0)
    parser.add_argument("--origin-lock", action="store_true")
    parser.add_argument("--ops-budget", action="store_true")
    parser.add_argument("--report", type=Path, default=None, help="write the results as JSON")
    args = parser.parse_args(argv)

    if not args.base_url:
        print("error: pass --base-url or set PUBLIC_BASE_URL", file=sys.stderr)
        return 2
    if not 1 <= args.max_requests <= HARD_MAX_REQUESTS:
        print(f"error: --max-requests must be 1-{HARD_MAX_REQUESTS}", file=sys.stderr)
        return 2
    try:
        client = Client(args.base_url, label="site", max_requests=args.max_requests,
                        timeout=60.0)
    except OpsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    report = Report("Post-deploy smoke")
    Smoke(client, report, args).run()
    report.add("budget", "PASS" if client.sent <= args.max_requests else "FAIL",
               f"{client.sent} of {args.max_requests} requests")
    report.write_summary(os.environ.get("GITHUB_STEP_SUMMARY"))
    if args.report:
        args.report.write_text(json.dumps(
            {"requests": client.sent, "results": [r.__dict__ for r in report.results]},
            indent=2) + "\n")
    print(f"smoke: {report.failed} failed, {client.sent} requests", flush=True)
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
