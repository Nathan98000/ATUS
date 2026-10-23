"""A small, quota-budgeted HTTP client for checks against the deployed site.

Standard library only, so the scheduled monitors run on a bare GitHub runner
without installing anything. Used by ``scripts/ops/monitors.py``,
``render_deploy.py``, ``deploy_smoke.py`` and ``scripts/replay_deployed.py``.

Rules every caller relies on:

* **A hard request budget.** Request N+1 raises :class:`BudgetExceeded`
  before anything is sent; a retry is a request like any other. Free-tier
  quotas are what the budget protects (ADR-011 "Unknowns", brief §16).
* **No secret is ever printed.** Messages name a target by its label
  (``site``, ``origin``) plus the request path: never the base URL (the
  Render origin is not published), a query string or a header value.
* **Redirects are never followed**; a 3xx comes back as the reply.
* **Plain HTTP only to loopback hosts**, and never credentials in a URL.
"""

from __future__ import annotations

import gzip
import ipaddress
import json
import math
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

USER_AGENT = "atus-ops/1"
LOOPBACK_NAMES = frozenset({"localhost"})
#: Largest body read from any reply; deployed replies are a few KB (ADR-011).
MAX_BODY_BYTES = 4 * 1024 * 1024


class OpsError(Exception):
    """A check could not be carried out or failed; the message is safe to print."""


class BudgetExceeded(OpsError):
    """The request budget is spent; nothing more was sent."""


class TransportError(OpsError):
    """No HTTP reply (connection refused or reset, DNS, timeout, TLS)."""


def is_loopback(host: str) -> bool:
    if host.lower() in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def validate_base_url(url: str, *, what: str = "base URL") -> str:
    """``url`` without a trailing slash; refuses anything but https (or http to loopback).

    The error messages never repeat the URL itself, because some base URLs
    (the Render origin) are secrets.
    """
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
    except ValueError as exc:
        raise OpsError(f"{what} is not a valid URL") from exc
    if parts.scheme not in ("https", "http") or not host:
        raise OpsError(f"{what} must be an https:// URL")
    if parts.username is not None or parts.password is not None:
        raise OpsError(f"{what} must not contain credentials")
    if parts.query or parts.fragment:
        raise OpsError(f"{what} must not contain a query string or fragment")
    if parts.scheme == "http" and not is_loopback(host):
        raise OpsError(f"{what}: plain HTTP is allowed only to loopback hosts")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Seconds from a ``Retry-After`` header (delta-seconds or an HTTP date)."""
    if value is None or not value.strip():
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return max(0.0, when - (time.time() if now is None else now))


@dataclass
class Reply:
    method: str
    path: str
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed_s: float

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    @property
    def content_type(self) -> str:
        return (self.header("content-type") or "").split(";", 1)[0].strip().lower()

    @property
    def is_json(self) -> bool:
        kind = self.content_type
        return kind == "application/json" or kind.endswith("+json")

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpsError(f"{self.method} {self.path}: the body is not JSON") from exc

    @property
    def error_code(self) -> str | None:
        """The v1 envelope's ``error.code``, when the body is one."""
        if not self.is_json:
            return None
        try:
            payload = json.loads(self.body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            code = payload["error"].get("code")
            return code if isinstance(code, str) else None
        return None

    @property
    def retry_after_s(self) -> float | None:
        return parse_retry_after(self.header("retry-after"))

    def describe(self) -> str:
        code = self.error_code
        return f"HTTP {self.status}" + (f" {code}" if code else "")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return a 3xx as the reply instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class Client:
    """Budgeted requests to one base URL, logged by ``label`` and path only."""

    def __init__(
        self,
        base_url: str,
        *,
        label: str,
        max_requests: int,
        timeout: float = 30.0,
        user_agent: str = USER_AGENT,
        log: Callable[[str], None] = _stderr,
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_requests < 0:
            raise OpsError("the request budget cannot be negative")
        self.base_url = validate_base_url(base_url, what=f"{label} URL")
        self.label = label
        self.max_requests = max_requests
        self.timeout = timeout
        self.user_agent = user_agent
        self.log = log
        # Looked up at call time, so tests can replace time.sleep.
        self.sleep = sleep if sleep is not None else (lambda seconds: time.sleep(seconds))
        self.monotonic = monotonic
        self.sent = 0
        self._opener = urllib.request.build_opener(NoRedirect)

    @property
    def remaining(self) -> int:
        return self.max_requests - self.sent

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Reply:
        if not path.startswith("/") or "://" in path:
            raise OpsError(f"request path must start with '/': {path[:80]!r}")
        if self.sent >= self.max_requests:
            raise BudgetExceeded(
                f"request budget of {self.max_requests} for {self.label} is spent; "
                f"not sending {method} {path}"
            )
        self.sent += 1
        request = urllib.request.Request(self.base_url + path, data=body, method=method)
        request.add_header("User-Agent", self.user_agent)
        request.add_header("Accept", "application/json, text/html;q=0.9, */*;q=0.1")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        started = self.monotonic()
        try:
            try:
                response = self._opener.open(request, timeout=timeout or self.timeout)
            except urllib.error.HTTPError as exc:  # 4xx/5xx/3xx: still a reply
                response = exc
            with response:
                status = response.status if hasattr(response, "status") else response.code
                reply_headers = {k.lower(): v for k, v in response.headers.items()}
                raw = response.read(MAX_BODY_BYTES + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            elapsed = self.monotonic() - started
            reason = getattr(exc, "reason", exc)
            kind = type(reason).__name__ if not isinstance(reason, str) else "URLError"
            self.log(f"{self.label} {method} {path} -> no reply ({kind}, {elapsed:.1f} s)")
            raise TransportError(f"{self.label} {method} {path}: no reply ({kind})") from None
        elapsed = self.monotonic() - started
        if len(raw) > MAX_BODY_BYTES:
            raise OpsError(f"{self.label} {method} {path}: reply larger than {MAX_BODY_BYTES} B")
        if reply_headers.get("content-encoding", "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except OSError as exc:
                raise OpsError(f"{self.label} {method} {path}: corrupt gzip body") from exc
        reply = Reply(method, path, status, reply_headers, raw, elapsed)
        self.log(f"{self.label} {method} {path} -> {reply.describe()} ({elapsed:.1f} s)")
        return reply

    def get(self, path: str, **kwargs: Any) -> Reply:
        return self.request("GET", path, **kwargs)

    def post_json(self, path: str, body: bytes, **kwargs: Any) -> Reply:
        headers = {"Content-Type": "application/json", **(kwargs.pop("headers", None) or {})}
        return self.request("POST", path, body=body, headers=headers, **kwargs)

    def wait_for(
        self,
        method: str,
        path: str,
        accept: Callable[[Reply], bool],
        *,
        deadline_s: float,
        min_interval_s: float = 15.0,
        max_attempts: int = 10,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Reply:
        """Repeat a request until ``accept(reply)``, honouring ``Retry-After``.

        For cold starts: a sleeping Render instance or a waking Worker origin
        answers 503 (``service_waking``) or nothing at all for about a minute.
        Each attempt counts against the budget; at most ``max_attempts`` are
        sent, never after ``deadline_s``. Raises :class:`OpsError` with the last
        outcome when the reply never becomes acceptable.
        """
        start = self.monotonic()
        last = "no attempt"
        for attempt in range(1, max_attempts + 1):
            try:
                reply = self.request(method, path, body=body, headers=headers, timeout=timeout)
            except TransportError as exc:
                last, delay = str(exc), min_interval_s
            else:
                if accept(reply):
                    return reply
                last = reply.describe()
                delay = max(min_interval_s, reply.retry_after_s or 0.0)
            remaining = deadline_s - (self.monotonic() - start)
            if attempt == max_attempts or remaining <= 0:
                break
            self.sleep(min(delay, max(0.0, remaining)))
        raise OpsError(
            f"{self.label} {method} {path} not ready after {attempt} attempt(s) "
            f"within {deadline_s:.0f} s; last: {last}"
        )


# --------------------------------------------------------------------------- #
# security headers                                                             #
# --------------------------------------------------------------------------- #

def _csp_ok(value: str) -> bool:
    return "frame-ancestors 'none'" in value and "default-src 'none'" in value


#: The static site (frontend/public/_headers, ADR-011 / brief §11).
SITE_HEADERS: dict[str, Callable[[str], bool]] = {
    "content-security-policy": _csp_ok,
    "strict-transport-security": lambda v: "max-age=" in v,
    "x-content-type-options": lambda v: v.strip().lower() == "nosniff",
    "referrer-policy": lambda v: v.strip().lower() == "no-referrer",
    "permissions-policy": lambda v: "camera=()" in v,
}

#: Every /api response the Worker sends (edge/src/http.ts SECURITY_HEADERS).
API_HEADERS: dict[str, Callable[[str], bool]] = {
    **SITE_HEADERS,
    "cross-origin-resource-policy": lambda v: v.strip().lower() == "same-origin",
    "cache-control": lambda v: "no-store" in v.lower(),
}


def header_problems(reply: Reply, rules: Mapping[str, Callable[[str], bool]]) -> list[str]:
    """Names of the headers in ``rules`` that are missing or have a wrong value."""
    problems = []
    for name, ok in rules.items():
        value = reply.header(name)
        if value is None:
            problems.append(f"{name} missing")
        elif not ok(value):
            problems.append(f"{name} unexpected")
    return problems


# --------------------------------------------------------------------------- #
# reporting                                                                    #
# --------------------------------------------------------------------------- #

@dataclass
class CheckResult:
    name: str
    status: str  # PASS | FAIL | WARN | SKIP
    detail: str = ""


class Report:
    """PASS/FAIL lines on stdout, plus a Markdown table in the job summary."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.results: list[CheckResult] = []

    def add(self, name: str, status: str, detail: str = "") -> CheckResult:
        result = CheckResult(name, status, detail)
        self.results.append(result)
        print(f"[{status}] {name}" + (f": {detail}" if detail else ""), flush=True)
        return result

    def ok(self, name: str, detail: str = "") -> None:
        self.add(name, "PASS", detail)

    def fail(self, name: str, detail: str) -> None:
        self.add(name, "FAIL", detail)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "FAIL")

    def markdown(self) -> str:
        lines = [f"### {self.title}", "", "| Check | Result | Detail |", "|---|---|---|"]
        for r in self.results:
            detail = r.detail.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {r.name} | {r.status} | {detail} |")
        return "\n".join(lines) + "\n"

    def write_summary(self, path: str | None) -> None:
        if path:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(self.markdown() + "\n")


def ceil_div(a: float, b: float) -> int:
    return int(math.ceil(a / b))
