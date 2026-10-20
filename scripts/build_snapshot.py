#!/usr/bin/env python3
"""Build the static result snapshot (tier L0, ADR-011 / ADR-015) from a running API.

The snapshot is a set of static files published with the SPA. Static files
never invoke the edge Worker, so metadata and the prewarmed analyses keep
working when Render or Neon sleep or pause, and when the Worker's daily
request allowance is spent. Layout (under ``--out``, e.g. ``frontend/dist/snap``):

    current.json                     no-cache pointer: snapshot_id, data_version,
                                     analytics_version, api_version, response_format,
                                     generated_at
    <snapshot_id>/manifest.json      immutable: the same fields plus
                                     "get":   {"/api/v1/meta": "get/meta.json", ...}
                                     "post":  {"<k>": {"path", "file", "analysis_key", "x_cache"}}
                                     "files": {"<file>": {"sha256", "bytes"}}
    <snapshot_id>/get/...            exact GET response bodies (all metadata and every
                                     activity detail)
    <snapshot_id>/post/<k>.json      exact analysis response bodies

where ``k = sha256_hex(path + "?" + query + "\\n" + body)`` (the request hash the
SPA and the Worker compute; the query is empty) and
``SNAPSHOT_ID = <api_build[:12]>-<sha256_hex(DATA_VERSION)[:16]>``.

Usage (CI runs the deployed image read-only against the restored release dump):

    python scripts/build_snapshot.py --api-base http://127.0.0.1:8571 \\
        --edge-secret-file ~/.config/atus/edge.env \\
        --requests-script frontend/scripts/snapshot-requests.mjs \\
        --api-build "$GITHUB_SHA" --out frontend/dist/snap

    python scripts/build_snapshot.py --verify frontend/dist/snap

``--requests FILE`` (repeatable) adds a JSON list ``[{"path", "body"[, "name", "key"]}]``
whose ``body`` strings are sent unchanged; ``--requests-script`` runs the SPA's
own request generator (two passes: the prewarm list from /meta and
/activities, then the canonical specs of those responses), so the bytes match
what the SPA sends. The edge secret comes from ``--edge-secret-file`` (a bare
value, or the ``EDGE_AUTH_SECRET=...`` line of the file
scripts/ops/gen_edge_secrets.sh writes) or the ``EDGE_AUTH_SECRET`` environment
variable; it is sent only as ``X-ATUS-Edge-Auth``, never printed, and plain
HTTP is refused except to loopback hosts. Redirects are never followed.

Every response must be a 200 with a JSON body; anything else (an error
envelope, an HTML page, an analysis computed during a data reload, i.e.
``X-Cache: bypass``) fails the build, except that 429/503 answers with
``Retry-After`` are retried within ``--max-wait`` seconds in total. Run the
API with generous rate limits for the build (for example
``ATUS_API_RATE_ANALYSIS=1000/60 ATUS_API_RATE_REQUESTS=10000/60``). All
responses must report one data version, analytics version and response
format. Files are written to a temporary directory, every file's sha256 is
verified against the manifest, and only then is the snapshot moved into place
and ``current.json`` written. Prints the snapshot id on stdout (progress goes
to stderr).

Exit status: 0 success, 1 build or verification failure, 2 usage error.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

ANALYSIS_PATHS = frozenset(
    {"/api/v1/analysis/estimate", "/api/v1/analysis/trend", "/api/v1/analysis/compare"}
)
METADATA_PATHS = (
    ("/api/v1/meta", "get/meta.json"),
    ("/api/v1/activities", "get/activities.json"),
    ("/api/v1/activities/presets", "get/activities/presets.json"),
    ("/api/v1/population/metadata", "get/population/metadata.json"),
)
ACTIVITY_CODE = re.compile(r"^[0-9]{2}(?:[0-9]{2}){0,2}$")
SNAPSHOT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
API_BUILD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
USER_AGENT = "atus-snapshot/1"
LOOPBACK_HOSTS = frozenset({"localhost"})


class SnapshotError(Exception):
    """The snapshot cannot be built or verified (message is safe to print)."""


def request_key(path: str, query: str, body: bytes) -> str:
    """``sha256_hex(path + "?" + query + "\\n" + body)``: the SPA's and the Worker's key."""
    return hashlib.sha256(f"{path}?{query}\n".encode() + body).hexdigest()


def snapshot_id(api_build: str, data_version: str) -> str:
    """``<api_build[:12]>-<sha256_hex(DATA_VERSION)[:16]>``."""
    return f"{api_build[:12]}-{hashlib.sha256(data_version.encode()).hexdigest()[:16]}"


# --------------------------------------------------------------------------- #
# inputs                                                                       #
# --------------------------------------------------------------------------- #


def read_edge_secret(path: Path | None) -> str | None:
    """The edge secret from a file (bare value or ``EDGE_AUTH_SECRET=`` line) or the env."""
    if path is None:
        value = os.environ.get("EDGE_AUTH_SECRET", "").strip()
        return value or None
    try:
        text = path.expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise SnapshotError(f"cannot read the edge secret file: {exc.strerror}") from None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lines = [line for line in lines if not line.startswith("#")]
    for line in lines:
        if line.startswith("EDGE_AUTH_SECRET="):
            value = line.partition("=")[2].strip()
            break
    else:
        if len(lines) != 1 or "=" in lines[0]:
            raise SnapshotError(
                "the edge secret file must hold the secret alone or an EDGE_AUTH_SECRET= line"
            )
        value = lines[0]
    if not value or not value.isascii() or not value.isprintable() or " " in value:
        raise SnapshotError("the edge secret must be visible ASCII without spaces")
    return value


def validate_api_base(base: str) -> str:
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SnapshotError("--api-base must be an http(s) URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise SnapshotError("--api-base must not carry credentials, a query or a fragment")
    if parts.scheme == "http" and not _is_loopback(parts.hostname):
        raise SnapshotError("plain http is accepted only for loopback hosts (the edge secret)")
    return base.rstrip("/")


def _is_loopback(host: str) -> bool:
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class AnalysisRequest:
    name: str
    path: str
    body: bytes
    key: str


def parse_request_list(payload: object, source: str) -> list[AnalysisRequest]:
    """Validate ``[{"path", "body"[, "name", "key"]}]`` (a list from the SPA's generator)."""
    if not isinstance(payload, list):
        raise SnapshotError(f"{source}: the request list must be a JSON array")
    requests: list[AnalysisRequest] = []
    for index, item in enumerate(payload):
        where = f"{source}[{index}]"
        if not isinstance(item, dict):
            raise SnapshotError(f"{where}: each request must be an object")
        path, body = item.get("path"), item.get("body")
        if path not in ANALYSIS_PATHS:
            raise SnapshotError(f"{where}: path must be one of {sorted(ANALYSIS_PATHS)}")
        if not isinstance(body, str):
            raise SnapshotError(f"{where}: body must be the exact request body as a string")
        raw = body.encode("utf-8")
        key = request_key(path, "", raw)
        if "key" in item and item["key"] != key:
            raise SnapshotError(
                f"{where}: key {item['key']!r} is not sha256(path + '?\\n' + body); "
                "the list and this builder disagree on the request hash"
            )
        name = item.get("name")
        requests.append(AnalysisRequest(str(name) if name else path, path, raw, key))
    return requests


def load_request_file(path: Path) -> list[AnalysisRequest]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotError(f"cannot read request list {path}: {exc}") from None
    return parse_request_list(payload, str(path))


def run_request_script(script: Path, args: list[str], workdir: Path) -> list[AnalysisRequest]:
    """Run the SPA's request generator (``node <script> <args>``) and parse its list.

    It runs in ``workdir`` (the build's temporary directory), so a relative
    ``script`` is resolved against the caller's working directory first.
    """
    node = shutil.which("node")
    if node is None:
        raise SnapshotError("--requests-script needs node on PATH")
    script = script.resolve()
    if not script.is_file():
        raise SnapshotError(f"--requests-script {script} is not a file")
    completed = subprocess.run(
        [node, str(script), *args],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        # The last line that is neither a stack frame nor Node's own
        # "Node.js v<version>" trailer: the error message.
        lines = [line for line in completed.stderr.strip().splitlines()
                 if line.strip() and not line.lstrip().startswith("at ")
                 and not line.startswith("Node.js v")]
        raise SnapshotError(f"{script.name} {args[0]} failed: {(lines or ['no output'])[-1]}")
    try:
        payload = json.loads(completed.stdout)
    except ValueError as exc:
        raise SnapshotError(f"{script.name} printed invalid JSON: {exc}") from None
    return parse_request_list(payload, f"{script.name} {args[0]}")


# --------------------------------------------------------------------------- #
# HTTP                                                                         #
# --------------------------------------------------------------------------- #


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would resend the edge secret wherever it points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class ApiResponse:
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass
class ApiClient:
    base: str
    edge_secret: str | None
    timeout_s: float = 300.0
    max_wait_s: float = 600.0
    sleep: Callable[[float], None] | None = None  # time.sleep
    waited_s: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        # No proxies from the environment either: the edge secret goes straight to the API.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _send(self, method: str, path: str, body: bytes | None) -> ApiResponse:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
            "X-Request-ID": f"snapshot-{secrets.token_hex(8)}",
        }
        if self.edge_secret is not None:
            headers["X-ATUS-Edge-Auth"] = self.edge_secret
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path, data=body, method=method, headers=headers
        )
        try:
            with self._opener.open(request, timeout=self.timeout_s) as response:
                return _read(response.status, response.headers, response.read())
        except urllib.error.HTTPError as exc:
            return _read(exc.code, exc.headers, exc.read())
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise SnapshotError(f"{method} {path}: cannot reach the API ({reason})") from None

    def fetch(self, method: str, path: str, body: bytes | None = None) -> ApiResponse:
        """A 200 JSON response, retrying 429/503 with Retry-After within the wait budget."""
        while True:
            response = self._send(method, path, body)
            if response.status == 200 and _is_json(response.headers.get("content-type")):
                return response
            retry = _retry_after(response)
            # At least a second per retry, so the wait budget also bounds the attempts.
            retry = None if retry is None else max(1, retry)
            if retry is not None and self.waited_s + retry <= self.max_wait_s:
                self.waited_s += retry
                (self.sleep or time.sleep)(retry)
                continue
            raise SnapshotError(f"{method} {path}: refused {_describe(response)}")


def _read(status: int, headers, raw: bytes) -> ApiResponse:
    lowered = {name.lower(): value for name, value in headers.items()}
    encoding = lowered.get("content-encoding", "identity").strip().lower()
    if encoding == "gzip":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError):
            raise SnapshotError(f"HTTP {status}: a gzip body that does not decode") from None
    elif encoding not in ("", "identity"):
        raise SnapshotError(f"unsupported Content-Encoding {encoding!r}")
    return ApiResponse(status, lowered, raw)


def _is_json(content_type: str | None) -> bool:
    return (content_type or "").split(";", 1)[0].strip().lower() == "application/json"


def _retry_after(response: ApiResponse) -> int | None:
    if response.status not in (429, 503):
        return None
    value = response.headers.get("retry-after", "").strip()
    return int(value) if value.isdigit() else None


def _describe(response: ApiResponse) -> str:
    """Status plus the API error code (never the body, which may echo the request)."""
    code = None
    if _is_json(response.headers.get("content-type")):
        try:
            payload = json.loads(response.body)
            code = payload.get("error", {}).get("code") if isinstance(payload, dict) else None
        except (ValueError, AttributeError):
            code = None
    kind = f"error code {code!r}" if isinstance(code, str) else (
        f"Content-Type {response.headers.get('content-type')!r}"
    )
    return f"HTTP {response.status} ({kind})"


# --------------------------------------------------------------------------- #
# build                                                                        #
# --------------------------------------------------------------------------- #


@dataclass
class Collected:
    """Responses gathered for one snapshot, with the version headers they carried."""

    files: dict[str, bytes] = field(default_factory=dict)
    get: dict[str, str] = field(default_factory=dict)
    post: dict[str, dict[str, object]] = field(default_factory=dict)
    seen: dict[str, set[str]] = field(
        default_factory=lambda: {"data_version": set(), "analytics_version": set(),
                                 "response_format": set()}
    )

    def note_versions(self, response: ApiResponse, analysis: bool) -> None:
        for header, name in (("x-data-version", "data_version"),
                             ("x-analytics-version", "analytics_version")):
            if response.headers.get(header):
                self.seen[name].add(response.headers[header])
        if analysis and response.headers.get("x-response-format"):
            self.seen["response_format"].add(response.headers["x-response-format"])


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _one(values: set[str], expected: str | None, name: str, flag: str) -> str:
    if len(values) > 1:
        raise SnapshotError(f"responses disagree on {name}: {sorted(values)}")
    if expected is not None:
        if values and values != {expected}:
            raise SnapshotError(f"the API reports {name} {sorted(values)[0]!r}, not {expected!r}")
        return expected
    if not values:
        raise SnapshotError(f"the API reported no {name}; pass {flag}")
    return next(iter(values))


def collect(
    client: ApiClient,
    requests: list[AnalysisRequest],
    request_script: Path | None,
    workdir: Path,
) -> tuple[Collected, dict]:
    collected = Collected()
    for path, file in METADATA_PATHS:
        response = client.fetch("GET", path)
        collected.note_versions(response, analysis=False)
        collected.files[file] = response.body
        collected.get[path] = file
    meta = json.loads(collected.files["get/meta.json"])
    activities = json.loads(collected.files["get/activities.json"])
    codes = sorted({str(item.get("code")) for item in activities.get("activities", [])})
    bad = [code for code in codes if not ACTIVITY_CODE.match(code)]
    if bad or not codes:
        raise SnapshotError(f"unexpected activity codes in /api/v1/activities: {bad[:3]}")
    for code in codes:
        path, file = f"/api/v1/activities/{code}", f"get/activities/{code}.json"
        response = client.fetch("GET", path)
        collected.note_versions(response, analysis=False)
        collected.files[file] = response.body
        collected.get[path] = file
    _log(f"snapshot: {len(collected.get)} metadata responses")

    def run_analyses(batch: list[AnalysisRequest]) -> list[dict]:
        responses = []
        for request in batch:
            if request.key in collected.post:
                continue
            response = client.fetch("POST", request.path, request.body)
            if response.headers.get("x-cache") == "bypass":
                # Computed while the data changed under the API: not tied to
                # one data version (the edge's KV store gate refuses it too).
                raise SnapshotError(
                    f"POST {request.path}: computed during a data reload (X-Cache: bypass)"
                )
            collected.note_versions(response, analysis=True)
            file = f"post/{request.key}.json"
            collected.files[file] = response.body
            collected.post[request.key] = {
                "path": request.path,
                "file": file,
                "analysis_key": response.headers.get("x-analysis-key"),
                "x_cache": response.headers.get("x-cache"),
            }
            responses.append({"path": request.path, "response": json.loads(response.body)})
        return responses

    first = list(requests)
    if request_script is not None:
        meta_file, activities_file = workdir / "meta.json", workdir / "activities.json"
        meta_file.write_bytes(collected.files["get/meta.json"])
        activities_file.write_bytes(collected.files["get/activities.json"])
        first += run_request_script(
            request_script,
            ["--meta", str(meta_file), "--activities", str(activities_file)],
            workdir,
        )
    answered = run_analyses(first)
    if request_script is not None:
        (workdir / "responses.json").write_text(json.dumps(answered), encoding="utf-8")
        run_analyses(run_request_script(
            request_script, ["--canonical", str(workdir / "responses.json")], workdir
        ))
    _log(f"snapshot: {len(collected.post)} analyses")
    return collected, meta


def build(args: argparse.Namespace) -> str:
    base = validate_api_base(args.api_base)
    secret = read_edge_secret(args.edge_secret_file)
    client = ApiClient(base, secret, timeout_s=args.timeout, max_wait_s=args.max_wait)
    requests: list[AnalysisRequest] = []
    for path in args.requests:
        requests += load_request_file(path)

    api_build = args.api_build
    if api_build is None:
        health = client.fetch("GET", "/health")
        api_build = json.loads(health.body).get("build")
    if not isinstance(api_build, str) or not API_BUILD.match(api_build):
        raise SnapshotError("no usable API build id: pass --api-build (the image's git SHA)")

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=out) as tmp:
        work = Path(tmp)
        collected, meta = collect(client, requests, args.requests_script, work)
        data_version = _one(collected.seen["data_version"], args.data_version,
                            "data_version", "--data-version")
        analytics_version = _one(
            collected.seen["analytics_version"] | {str(meta.get("analytics_version"))},
            None, "analytics_version", "",
        )
        response_format = _one(collected.seen["response_format"], args.response_format,
                               "response_format", "--response-format")
        api_version = str(meta.get("api_version"))
        sid = snapshot_id(api_build, data_version)
        if not SNAPSHOT_ID.match(sid):
            raise SnapshotError(f"invalid snapshot id {sid!r}")

        generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        pointer = {
            "snapshot_id": sid,
            "data_version": data_version,
            "analytics_version": analytics_version,
            "api_version": api_version,
            "response_format": response_format,
            "generated_at": generated_at,
        }
        staged = work / sid
        for file, body in collected.files.items():
            target = staged / file
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        manifest = {
            **pointer,
            "api_build": api_build[:12],
            "get": dict(sorted(collected.get.items())),
            "post": dict(sorted(collected.post.items())),
            "files": {
                file: {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
                for file, body in sorted(collected.files.items())
            },
        }
        (staged / "manifest.json").write_text(
            json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        verify_snapshot_dir(staged)

        final = out / sid
        if final.exists():
            shutil.rmtree(final)
        staged.rename(final)
        pointer_tmp = work / "current.json"
        pointer_tmp.write_text(json.dumps(pointer, indent=1) + "\n", encoding="utf-8")
        pointer_tmp.replace(out / "current.json")
    verify(out)
    _log(f"snapshot: wrote {out / sid} ({len(collected.files)} files)")
    return sid


# --------------------------------------------------------------------------- #
# verification                                                                 #
# --------------------------------------------------------------------------- #


def _safe_file(name: object) -> bool:
    return (
        isinstance(name, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}", name) is not None
        and ".." not in name.split("/")
    )


def verify_snapshot_dir(directory: Path) -> dict:
    """Check one ``<snapshot_id>/`` directory against its manifest; returns the manifest."""
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotError(f"unreadable manifest in {directory.name}: {exc}") from None
    if manifest.get("snapshot_id") != directory.name:
        raise SnapshotError("manifest snapshot_id does not match its directory")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise SnapshotError("manifest lists no files")
    referenced = set(manifest.get("get", {}).values())
    for key, entry in manifest.get("post", {}).items():
        if not re.fullmatch(r"[0-9a-f]{64}", key) or entry.get("path") not in ANALYSIS_PATHS:
            raise SnapshotError(f"invalid post entry {key[:16]}")
        referenced.add(entry.get("file"))
    if referenced != set(files):
        raise SnapshotError("manifest get/post entries and its file list differ")
    on_disk = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    if on_disk != set(files):
        extra = sorted(on_disk ^ set(files))[:3]
        raise SnapshotError(f"files on disk differ from the manifest: {extra}")
    for name, entry in files.items():
        if not _safe_file(name):
            raise SnapshotError(f"unsafe file name in manifest: {name!r}")
        body = (directory / name).read_bytes()
        if hashlib.sha256(body).hexdigest() != entry.get("sha256") or len(body) != entry.get(
            "bytes"
        ):
            raise SnapshotError(f"{name}: sha256 does not match the manifest")
    return manifest


def verify(out: Path) -> str:
    """Verify ``current.json`` and the snapshot it points to; returns the snapshot id."""
    try:
        pointer = json.loads((out / "current.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotError(f"unreadable current.json: {exc}") from None
    sid = pointer.get("snapshot_id")
    if not isinstance(sid, str) or not SNAPSHOT_ID.match(sid):
        raise SnapshotError("current.json has no valid snapshot_id")
    manifest = verify_snapshot_dir(out / sid)
    for name in ("data_version", "analytics_version", "api_version", "response_format"):
        if manifest.get(name) != pointer.get(name):
            raise SnapshotError(f"current.json and the manifest disagree on {name}")
    return sid


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build or verify the static result snapshot (ADR-015).",
    )
    parser.add_argument("--api-base", help="base URL of the running API, e.g. http://127.0.0.1:8571")
    parser.add_argument("--out", type=Path, help="output directory, e.g. frontend/dist/snap")
    parser.add_argument("--requests", type=Path, action="append", default=[],
                        help="JSON request list [{path, body}] (repeatable)")
    parser.add_argument("--requests-script", type=Path,
                        help="the SPA's generator, frontend/scripts/snapshot-requests.mjs")
    parser.add_argument("--edge-secret-file", type=Path,
                        help="file holding EDGE_AUTH_SECRET (else the environment variable)")
    parser.add_argument("--api-build", help="the API image's git SHA (else /health 'build')")
    parser.add_argument("--data-version", help="expected data version (else X-Data-Version)")
    parser.add_argument("--response-format", default="1",
                        help="expected X-Response-Format (default 1)")
    parser.add_argument("--timeout", type=float, default=300.0, help="per-request timeout, s")
    parser.add_argument("--max-wait", type=float, default=600.0,
                        help="total seconds to wait on 429/503 Retry-After")
    parser.add_argument("--verify", type=Path, metavar="DIR",
                        help="only verify an existing snapshot directory")
    args = parser.parse_args(argv)
    if args.verify is None and (args.api_base is None or args.out is None):
        parser.error("--api-base and --out are required (or use --verify DIR)")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        sid = verify(args.verify) if args.verify is not None else build(args)
    except SnapshotError as exc:
        _log(f"snapshot: error: {exc}")
        return 1
    print(sid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
