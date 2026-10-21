"""Comparison logic of the 3-way HTTP equivalence harness (brief §9).

The harness (``replay.py``) sends every request to three API servers:

* ``a`` -- the original Phase 1-4 API (commit 387fc2d) on the full database;
* ``b`` -- the Phase 5 API (this checkout) on the full database;
* ``c`` -- the Phase 5 API on the serving database (the restored release).

and compares the answers pairwise. This module holds everything that decides
whether two answers are equivalent; it does no I/O, so it is unit-tested on
its own (tests/unit/test_equivalence_harness.py).

Pairs and their rules
---------------------

``b_c`` (same code, full vs serving data: the §9 claim). Status codes must be
identical. A 2xx body is compared under the §9 criterion
(``atus_pipeline.serving.equivalence.compare_results``): every field exactly
equal (point estimates, counts, weighted populations, days, labels, warnings,
the echoed spec, ``analytics_version``) except ``standard_error``,
``ci_lower`` and ``ci_upper``, which may differ by at most ``1e-12 x max(|a|,
|b|, S)`` with ``S`` the estimate's own point value (and, for a comparison's
difference, both groups' values): the scale floor for zero-variance
estimates (audit F40/I12). An error body (status, code, message and details)
must be identical, byte for byte after JSON parsing. A few response headers
must be identical too (:data:`MATCHED_HEADERS`). The only fields allowed to
differ are listed in :data:`WHITELIST`, each with its justification.

``a_b`` (same full database, Phase 1-4 vs Phase 5 code: nothing Phase 5 did
changed a statistic or a domain answer). The same comparison; a difference
is accepted only when one of :data:`INTENDED_DIFFERENCES` explains it
exactly (each names the audit finding or contract item that made the
change). Domain errors (``invalid_spec``, ``unknown_activity``,
``unsupported_analysis``, ``insufficient_data``, ``invalid_analysis``) have no
such entry: their code and message must be identical.

A spec (or a GET request) passes when no pair has an unexplained
difference, every server answered (after retrying transient capacity
answers), and no Phase 5 server answered 5xx (a baseline 5xx is judged by
the a_b pair: Phase 5 fixed some, e.g. audit F50).
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from atus_pipeline.api.errors import validation_details
from atus_pipeline.serving.equivalence import REL_TOL, TOLERANT_FIELDS, compare_results

HARNESS_VERSION = 1

TARGETS = ("a", "b", "c")
TARGET_LABELS = {
    "a": "Phase 1-4 API (baseline commit) on the full database",
    "b": "Phase 5 API on the full database",
    "c": "Phase 5 API on the serving database",
}
PAIRS = {"a_b": ("a", "b"), "b_c": ("b", "c")}
PAIR_LABELS = {
    "a_b": "Phase 1-4 vs Phase 5 code, full database",
    "b_c": "full vs serving database, Phase 5 code",
}

ENDPOINTS = {
    "estimate": "/api/v1/analysis/estimate",
    "trend": "/api/v1/analysis/trend",
    "compare": "/api/v1/analysis/compare",
}
EXPECTATIONS = ("ok", "error", "any")  # "any": generated specs, not asserted
JSON_MEDIA_TYPE = "application/json"

DOMAIN_ERROR_CODES = frozenset({
    "invalid_spec", "unknown_activity", "unsupported_analysis", "insufficient_data",
    "invalid_analysis",
})

# Capacity answers that say "not now" rather than "no": the harness retries
# them (and a request that never got an HTTP answer). Anything else is final.
# analysis_timeout is not: at the harness's 10-minute statement timeout it
# has meant a query plan that runs for over 10 minutes again on every retry
# (two pandemic date-window specs on the full database, 2026-10-20), so it
# is reported at once, as a Phase 5 server error.
RETRYABLE_STATUSES = frozenset({429, 503})
RETRYABLE_CODES = frozenset({
    "rate_limited", "overloaded", "database_unavailable", "daily_capacity_reached",
})

# Response headers that must be identical on b and c (same code, so they can
# only differ if the data does). Header names are compared lower-case.
MATCHED_HEADERS = ("content-type", "cache-control", "x-analytics-version", "x-response-format")

OUTCOMES = ("equivalent", "intended_difference", "mismatch")
SPEC_STATUSES = ("equivalent", "intended_difference", "mismatch", "server_error",
                 "incomplete")
PASSING = frozenset({"equivalent", "intended_difference"})


class SpecError(ValueError):
    """The spec directory holds a malformed spec (message lists every problem)."""


# --------------------------------------------------------------------------- #
# specs                                                                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Spec:
    """One request to replay: a JSON line ``{id, endpoint, body, category,
    expect[, content_type, note]}``.

    ``endpoint`` is ``estimate``/``trend``/``compare`` or the full path
    (``/api/v1/analysis/estimate``). ``body`` is a JSON value serialized
    compactly (key order kept), or a string sent verbatim (for malformed-JSON
    cases). ``expect`` is what the spec's author expects the Phase 5 API on
    the full database to answer: ``ok`` (2xx), ``error`` (anything else),
    ``any`` (not asserted; generated specs), ``error:<code>``, or an object
    ``{"outcome": "ok"|"error"|"any", "status": 422, "code": "..."}`` whose
    ``status`` and ``code`` are optional (other keys are ignored). A miss is
    reported, not an equivalence failure.
    """

    id: str
    endpoint: str
    body: Any
    category: str
    expect: str                      # the outcome: ok, error or any
    source: str
    content_type: str = JSON_MEDIA_TYPE
    expect_status: int | None = None
    expect_code: str | None = None
    note: str | None = None

    def expectation(self) -> dict:
        out: dict = {"outcome": self.expect}
        if self.expect_status is not None:
            out["status"] = self.expect_status
        if self.expect_code is not None:
            out["code"] = self.expect_code
        return out

    def expectation_met(self, answer: Response) -> bool | None:
        """Whether ``answer`` is what the spec expects (None: not asserted,
        or no answer)."""
        if self.expect == "any" or answer.status is None:
            return None
        return (answer.ok == (self.expect == "ok")
                and self.expect_status in (None, answer.status)
                and self.expect_code in (None, answer.error_code))

    @property
    def path(self) -> str:
        return ENDPOINTS[self.endpoint]

    def body_bytes(self) -> bytes:
        if isinstance(self.body, str):
            return self.body.encode("utf-8")
        return json.dumps(self.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def request(self, max_body_bytes: int) -> RequestInfo:
        return RequestInfo("POST", self.path, self.body_bytes(), self.content_type,
                           max_body_bytes)


SPEC_SUFFIXES = (".jsonl", ".ndjson")
_ENDPOINT_NAMES = {path: name for name, path in ENDPOINTS.items()}


def parse_expectation(value: Any) -> tuple[str, int | None, str | None] | None:
    """``(outcome, status, code)`` of an ``expect`` value, or None if invalid."""
    if isinstance(value, str):
        outcome, colon, code = value.partition(":")
        if outcome in EXPECTATIONS and not colon:
            return outcome, None, None
        if outcome == "error" and code:
            return outcome, None, code
        return None
    if isinstance(value, dict) and value.get("outcome") in EXPECTATIONS:
        status, code = value.get("status"), value.get("code")
        if (status is None or (isinstance(status, int) and not isinstance(status, bool))) and (
                code is None or isinstance(code, str)):
            return value["outcome"], status, code
    return None
_SPEC_KEYS = ("id", "endpoint", "body", "expect")


def _spec_from_line(line: str, source: str) -> tuple[Spec | None, list[str]]:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        return None, [f"{source}: not JSON ({exc.msg} at column {exc.colno})"]
    if not isinstance(raw, dict):
        return None, [f"{source}: a spec line must be a JSON object"]
    problems = [f"{source}: missing {key!r}" for key in _SPEC_KEYS if key not in raw]
    spec_id = raw.get("id")
    if isinstance(spec_id, bool) or not isinstance(spec_id, (str, int)) or str(spec_id) == "":
        problems.append(f"{source}: 'id' must be a non-empty string or integer")
    endpoint = raw.get("endpoint")
    endpoint = _ENDPOINT_NAMES.get(endpoint, endpoint) if isinstance(endpoint, str) else None
    if endpoint not in ENDPOINTS:
        problems.append(f"{source}: 'endpoint' must be one of {', '.join(ENDPOINTS)} "
                        "(or its /api/v1/analysis/... path)")
    expectation = parse_expectation(raw.get("expect"))
    if "expect" in raw and expectation is None:
        problems.append(f"{source}: 'expect' must be one of {', '.join(EXPECTATIONS)}, "
                        "error:<code>, or {\"outcome\", \"status\"?, \"code\"?}")
    note = raw.get("note")
    if note is not None and not isinstance(note, str):
        problems.append(f"{source}: 'note' must be a string")
    category = raw.get("category", "uncategorized")
    if not isinstance(category, str) or not category:
        problems.append(f"{source}: 'category' must be a non-empty string")
    content_type = raw.get("content_type", JSON_MEDIA_TYPE)
    if not isinstance(content_type, str):
        problems.append(f"{source}: 'content_type' must be a string")
    if "body" in raw and not isinstance(raw["body"], str):
        try:
            json.dumps(raw["body"], allow_nan=False)
        except ValueError:
            problems.append(f"{source}: 'body' holds a non-finite number; send it as a string")
    if problems:
        return None, problems
    outcome, status, code = expectation
    return Spec(id=str(spec_id), endpoint=endpoint, body=raw["body"], category=category,
                expect=outcome, source=source, content_type=content_type,
                expect_status=status, expect_code=code, note=note), []


def load_specs(directory: Path) -> tuple[list[Spec], list[dict]]:
    """Every spec in ``directory``'s ``*.jsonl``/``*.ndjson`` files (sorted by
    file name, then line), and a description of each file (name, sha256,
    spec count). Raises :class:`SpecError` listing every problem: malformed
    lines, missing fields, duplicate ids (across files), no specs at all."""
    if not directory.is_dir():
        raise SpecError(f"{directory}: not a directory")
    files = sorted(p for p in directory.iterdir()
                   if p.is_file() and p.suffix in SPEC_SUFFIXES)
    specs: list[Spec] = []
    described: list[dict] = []
    problems: list[str] = []
    seen: dict[str, str] = {}
    for path in files:
        data = path.read_bytes()
        count = 0
        for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            source = f"{path.name}:{number}"
            spec, line_problems = _spec_from_line(line, source)
            problems += line_problems
            if spec is None:
                continue
            if spec.id in seen:
                problems.append(f"{source}: duplicate id {spec.id!r} (first at {seen[spec.id]})")
                continue
            seen[spec.id] = source
            specs.append(spec)
            count += 1
        described.append({"file": path.name, "sha256": hashlib.sha256(data).hexdigest(),
                          "specs": count})
    if not files:
        problems.append(f"{directory}: no {'/'.join(SPEC_SUFFIXES)} files")
    elif not specs and not problems:
        problems.append(f"{directory}: the spec files hold no specs")
    if problems:
        shown = problems[:50]
        more = f"\n... and {len(problems) - 50} more" if len(problems) > 50 else ""
        raise SpecError("invalid specs:\n" + "\n".join(shown) + more)
    return specs, described


# --------------------------------------------------------------------------- #
# requests and responses                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RequestInfo:
    """What was sent (the intended-difference rules look at it)."""

    method: str
    path: str                     # path and query string
    body: bytes = b""
    content_type: str | None = None
    max_body_bytes: int = 65_536  # the Phase 5 servers' ATUS_API_MAX_BODY_BYTES

    @property
    def media_type(self) -> str:
        return (self.content_type or "").split(";", 1)[0].strip().lower()

    def json_body(self) -> Any:
        try:
            return json.loads(self.body)
        except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
            return None


_NOT_JSON = object()


@dataclass
class Response:
    """One server's answer. ``status`` is None when no HTTP response arrived
    (``transport_error`` says why)."""

    status: int | None
    headers: dict[str, str] = field(default_factory=dict)   # lower-case names
    body: bytes = b""
    transport_error: str | None = None
    attempts: int = 1
    timed_out: bool = False          # no answer within the request timeout: not retried
    _parsed: Any = field(default=None, repr=False, compare=False)

    @property
    def payload(self) -> Any:
        """The parsed JSON body, or the module's ``_NOT_JSON`` marker."""
        if self._parsed is None:
            try:
                self._parsed = json.loads(self.body)
            except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
                self._parsed = _NOT_JSON
        return self._parsed

    @property
    def is_json(self) -> bool:
        return self.payload is not _NOT_JSON

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def error(self) -> dict | None:
        """The ``error`` object of an error envelope, if this is one."""
        payload = self.payload
        if (not self.ok and isinstance(payload, dict) and isinstance(payload.get("error"), dict)):
            return payload["error"]
        return None

    @property
    def error_code(self) -> str | None:
        error = self.error
        return error.get("code") if error else None

    def describe(self) -> str:
        if self.status is None:
            return f"no response ({self.transport_error})"
        return f"{self.status} {self.error_code}" if self.error_code else str(self.status)


def is_retryable(response: Response) -> bool:
    """Whether a server answered "not now" (or not at all): worth retrying.
    A request that timed out is not: it would most likely time out again."""
    if response.status is None:
        return not response.timed_out
    return response.status in RETRYABLE_STATUSES and response.error_code in RETRYABLE_CODES


# --------------------------------------------------------------------------- #
# whitelisted fields                                                           #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WhitelistRule:
    """A body key (matched at any depth) whose value may differ in a pair."""

    id: str
    key: str
    pairs: tuple[str, ...]
    justification: str

    def as_dict(self) -> dict:
        return {"id": self.id, "key": self.key, "pairs": list(self.pairs),
                "justification": self.justification}


WHITELIST: tuple[WhitelistRule, ...] = (
    WhitelistRule(
        id="data_version",
        key="data_version",
        pairs=("a_b", "b_c"),
        justification=(
            "The identity of a data build (`<release>:serving-<build_id>:<hash>` on the "
            "serving DB, `<release>:load-...` or the legacy `<release>:run<id>:...` on the "
            "full DB). It differs between every two databases by design and is not a "
            "statistic (docs/operations.md, equivalence criterion, 'What it does not "
            "cover'). No response body carries it today; the rule keeps the harness valid "
            "if /meta starts to."
        ),
    ),
    WhitelistRule(
        id="analysis_key",
        key="analysis_key",
        pairs=("a_b", "b_c"),
        justification=(
            "sha256 of operation + canonical spec + analytics version + data version: it "
            "differs exactly when data_version does. Sent as the X-Analysis-Key header, "
            "not in bodies, today."
        ),
    ),
)

# Headers allowed to differ in every pair (never compared): per-request or
# per-database identities and cache state.
WHITELISTED_HEADERS: dict[str, str] = {
    "x-data-version": "data build identity (see the data_version rule); checked instead "
                      "against the version each server reported at start",
    "x-analysis-key": "hash including the data version (see the analysis_key rule)",
    "x-request-id": "generated per request",
    "x-cache": "cache state (hit/miss/join) depends on request order, not on data",
    "server-timing": "timings",
    "date": "time of the response",
    "content-length": "follows the body, which is compared itself",
}

# Considered and deliberately compared (reported with the whitelist).
NOT_WHITELISTED: dict[str, str] = {
    "/meta data.last_ingested_at, data.ingestion_run_id, data.release": (
        "Ingestion provenance. The serving builder copies the source run's id, release "
        "and finish time into the serving DB's ingestion_runs row (ADR-013; only "
        "data_version and notes are new), so full and serving answer the same. A builder "
        "that stopped doing so is caught here, and a rule must then be added deliberately."
    ),
    "/meta data.respondents, data.years": (
        "Facts about the served data: equal by construction of the serving DB."
    ),
}


def _strip(a: Any, b: Any, keys: dict[str, str], path: str, notes: list[dict]
           ) -> tuple[Any, Any]:
    if isinstance(a, dict) and isinstance(b, dict):
        out_a, out_b = {}, {}
        for key in list(dict.fromkeys([*a, *b])):
            sub = f"{path}.{key}"
            if key in keys:
                left, right = a.get(key, "<absent>"), b.get(key, "<absent>")
                if left != right:
                    notes.append({"rule": keys[key], "path": sub, "left": left, "right": right})
                continue
            if key in a and key in b:
                out_a[key], out_b[key] = _strip(a[key], b[key], keys, sub, notes)
            elif key in a:
                out_a[key] = a[key]
            else:
                out_b[key] = b[key]
        return out_a, out_b
    if isinstance(a, list) and isinstance(b, list):
        pairs = [_strip(x, y, keys, f"{path}[{i}]", notes)
                 for i, (x, y) in enumerate(zip(a, b, strict=False))]
        return ([p[0] for p in pairs] + a[len(pairs):], [p[1] for p in pairs] + b[len(pairs):])
    return a, b


def strip_whitelisted(a: Any, b: Any, pair: str) -> tuple[Any, Any, list[dict]]:
    """``a`` and ``b`` without the keys whitelisted for ``pair`` (at any
    depth), and a note for every whitelisted value that differed."""
    keys = {rule.key: rule.id for rule in WHITELIST if pair in rule.pairs}
    notes: list[dict] = []
    left, right = _strip(a, b, keys, "$", notes)
    return left, right, notes


# --------------------------------------------------------------------------- #
# comparing two answers                                                        #
# --------------------------------------------------------------------------- #


def json_path(path: str) -> str:
    """``estimate.standard_error`` / ``points[2].year`` -> ``$.estimate...``."""
    if not path:
        return "$"
    return "$" + path if path.startswith("[") else "$." + path


def _exact(a: Any, b: Any, path: str, out: list[str]) -> None:
    """Exact comparison (same JSON types, same values) with paths."""
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            out.append(f"{path}: keys {sorted(set(a) ^ set(b))} differ")
        for key in sorted(set(a) & set(b)):
            _exact(a[key], b[key], f"{path}.{key}" if path else key, out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: {len(a)} vs {len(b)} items")
        for i, (x, y) in enumerate(zip(a, b, strict=False)):
            _exact(x, y, f"{path}[{i}]", out)
    elif type(a) is not type(b) or a != b:
        out.append(f"{path}: {a!r} vs {b!r}")


def _mismatch(text: str) -> dict:
    path, _, detail = text.partition(": ")
    return {"path": json_path(path), "detail": detail}


@dataclass
class PairResult:
    pair: str
    outcome: str = "equivalent"            # one of OUTCOMES
    mismatches: list[dict] = field(default_factory=list)   # {path, detail}
    classification: dict | None = None     # the intended difference, if one explains it
    whitelisted: list[dict] = field(default_factory=list)  # whitelisted values that differed
    compared: int = 0                      # leaf values compared
    max_relative: float = 0.0              # largest |a-b|/max(|a|,|b|) over SE/CI values
    max_scaled: float = 0.0                # largest |a-b|/max(|a|,|b|,S): the criterion's ratio

    def as_dict(self) -> dict:
        out = {"outcome": self.outcome, "compared": self.compared,
               "max_relative": _finite(self.max_relative),
               "max_scaled": _finite(self.max_scaled)}
        if self.mismatches:
            out["mismatches"] = self.mismatches[:25]
            if len(self.mismatches) > 25:
                out["mismatch_count"] = len(self.mismatches)
        if self.classification:
            out["classification"] = self.classification
        if self.whitelisted:
            out["whitelisted"] = self.whitelisted
        return out


def _finite(value: float) -> float | str:
    return value if math.isfinite(value) else "inf"


def compare_responses(pair: str, left: Response, right: Response, *,
                      expected_versions: dict[str, str] | None = None) -> PairResult:
    """Compare two HTTP answers under the pair's rules (no intended
    differences applied; see :func:`evaluate_pair`).

    ``expected_versions`` maps a target to the data version it reported at
    start; a response whose ``X-Data-Version`` differs from it was answered
    from other data than the run is about (a reload mid-run) and is a
    mismatch."""
    result = PairResult(pair)
    left_key, right_key = PAIRS[pair]
    for key, response in ((left_key, left), (right_key, right)):
        expected = (expected_versions or {}).get(key)
        served = response.headers.get("x-data-version")
        if expected and served and served != expected:
            result.mismatches.append({
                "path": "headers.x-data-version",
                "detail": f"{key} answered from data version {served!r}, not {expected!r}",
            })
    if left.status != right.status:
        result.mismatches.append({"path": "status",
                                  "detail": f"{left.describe()} vs {right.describe()}"})
    elif not (left.is_json and right.is_json):
        result.compared += 1
        if left.body != right.body:
            result.mismatches.append({"path": "$", "detail": "non-JSON bodies differ"})
    else:
        a, b, result.whitelisted = strip_whitelisted(left.payload, right.payload, pair)
        if left.ok:
            comparison = compare_results(a, b)
            result.compared += comparison.compared
            result.max_relative = comparison.max_relative
            result.max_scaled = comparison.max_scaled
            result.mismatches += [_mismatch(m) for m in comparison.mismatches]
        else:
            texts: list[str] = []
            _exact(a, b, "", texts)
            result.compared += 1
            result.mismatches += [_mismatch(m) for m in texts]
    if pair == "b_c":
        for name in MATCHED_HEADERS:
            if left.headers.get(name) != right.headers.get(name):
                result.mismatches.append({
                    "path": f"headers.{name}",
                    "detail": f"{left.headers.get(name)!r} vs {right.headers.get(name)!r}",
                })
    result.outcome = "mismatch" if result.mismatches else "equivalent"
    return result


# --------------------------------------------------------------------------- #
# intended Phase 5 differences (a_b only)                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IntendedDifference:
    """A deliberate Phase 5 change in what the API answers. ``applies`` gets
    (baseline answer, Phase 5 answer, request) and must recognize exactly
    that change, so it cannot hide anything else."""

    id: str
    source: str
    description: str
    applies: Callable[[Response, Response, RequestInfo], bool]

    def as_dict(self) -> dict:
        return {"id": self.id, "source": self.source, "description": self.description}


def _cap(details: Any) -> dict | None:
    """Phase 5's capping (api/errors.py ``validation_details``) applied to a
    baseline ``details.errors`` list."""
    if not isinstance(details, dict) or not isinstance(details.get("errors"), list):
        return None
    raw = []
    for error in details["errors"]:
        if not isinstance(error, dict):
            return None
        raw.append({"loc": error.get("location", ()), "msg": error.get("message", ""),
                    "type": error.get("type", "")})
    return validation_details(raw)


def _validation_capped(a: Response, b: Response, request: RequestInfo) -> bool:
    ea, eb = a.error, b.error
    return (
        a.status == b.status and a.status in (400, 422) and ea is not None and eb is not None
        and ea.get("code") == eb.get("code") in ("validation_error", "malformed_json")
        and ea.get("message") == eb.get("message")
        and set(ea) == set(eb) == {"code", "message", "details"}
        and ea.get("details") != eb.get("details")
        and _cap(ea.get("details")) == eb.get("details")
    )


_PRESET_LOCATION = ["body", "activity", "preset"]


def _error_list(error: dict | None) -> list[dict]:
    """``details.errors`` of an error object (only its well-formed entries)."""
    details = (error or {}).get("details")
    errors = details.get("errors") if isinstance(details, dict) else None
    return [e for e in errors if isinstance(e, dict)] if isinstance(errors, list) else []


def _preset_too_long(a: Response, b: Response, request: RequestInfo) -> bool:
    body = request.json_body()
    activity = body.get("activity") if isinstance(body, dict) else None
    preset = activity.get("preset") if isinstance(activity, dict) else None
    eb = b.error
    if not (isinstance(preset, str) and len(preset) > 64 and a.status == 422 and b.status == 422
            and eb is not None and eb.get("code") == "validation_error"):
        return False
    errors = _error_list(eb)
    flagged = [e for e in errors if e.get("location") == _PRESET_LOCATION
               and e.get("type") == "string_too_long"]
    if not flagged:
        return False
    ea = a.error
    if ea is not None and ea.get("code") == "validation_error":
        # The baseline also refused the schema: Phase 5's other problems must be its.
        def problems(errs):
            return {(tuple(e.get("location", ())), e.get("type")) for e in errs}

        rest = [e for e in errors if e not in flagged]
        return problems(rest) == problems(_error_list(ea))
    return True


def _payload_too_large(a: Response, b: Response, request: RequestInfo) -> bool:
    return (b.status == 413 and b.error_code == "payload_too_large"
            and len(request.body) > request.max_body_bytes)


def _unsupported_media_type(a: Response, b: Response, request: RequestInfo) -> bool:
    return (request.method == "POST" and b.status == 415
            and b.error_code == "unsupported_media_type"
            and request.media_type != JSON_MEDIA_TYPE)


def _parser_gave_up(a: Response, b: Response, request: RequestInfo) -> bool:
    eb = b.error or {}
    return (a.status == 400 and a.error_code == "http_400" and b.status == 400
            and eb.get("code") == "malformed_json"
            and eb.get("message") == "The request body could not be parsed as JSON.")


def _control_characters(a: Response, b: Response, request: RequestInfo) -> bool:
    query = urlsplit(request.path).query
    has_control = any(
        any(ord(ch) < 32 or ord(ch) == 127 for ch in name + value)
        for name, value in parse_qsl(query, keep_blank_values=True)
    )
    eb = b.error or {}
    return (has_control and b.status == 422 and eb.get("code") == "validation_error"
            and any(e.get("message") == "Control characters are not allowed."
                    for e in _error_list(eb)))


_LABELLED_LEXICON = re.compile(r" in the \S+ lexicon\.$")


def _lexicon_wording(a: Response, b: Response, request: RequestInfo) -> bool:
    ea, eb = a.error, b.error
    if not (
        a.status == b.status == 404 and ea is not None and eb is not None
        and ea.get("code") == eb.get("code") == "activity_not_found"
        and ea.get("details") == eb.get("details")
        and isinstance(eb.get("message"), str) and isinstance(ea.get("message"), str)
    ):
        return False
    if eb["message"].replace(" activity lexicon.", " lexicon.") == ea["message"]:
        return True
    # A malformed code (not 2, 4 or 6 digits) is answered release-neutrally,
    # whether or not the process has read its data snapshot (review r1).
    code = str((ea.get("details") or {}).get("code", ""))
    malformed = not (code.isdigit() and len(code) in (2, 4, 6))
    return malformed and eb["message"] == _LABELLED_LEXICON.sub(
        " in the activity lexicon.", ea["message"])


def _health_build(a: Response, b: Response, request: RequestInfo) -> bool:
    return (urlsplit(request.path).path == "/health" and a.status == b.status == 200
            and a.payload == {"status": "ok"} and isinstance(b.payload, dict)
            and set(b.payload) == {"status", "build"} and b.payload["status"] == "ok")


INTENDED_DIFFERENCES: tuple[IntendedDifference, ...] = (
    IntendedDifference(
        "validation_details_capped", "audit F04",
        "422/400 `details.errors` are capped at 20 entries with truncated strings "
        "(`details.truncated`/`error_count` added). Recognized only when Phase 5's own "
        "capping function applied to the baseline's details reproduces Phase 5's details "
        "exactly; code and message must be identical.",
        _validation_capped,
    ),
    IntendedDifference(
        "preset_max_length", "audit F04",
        "`activity.preset` longer than 64 characters is now a schema violation (422 "
        "`validation_error`, type `string_too_long`) instead of reaching the engine; the "
        "baseline also answered 422.",
        _preset_too_long,
    ),
    IntendedDifference(
        "payload_too_large", "audit F03",
        "Request bodies over ATUS_API_MAX_BODY_BYTES (65,536 B by default) are refused "
        "with 413 `payload_too_large` before parsing.",
        _payload_too_large,
    ),
    IntendedDifference(
        "unsupported_media_type", "ADR-012 / Phase 5 contract",
        "A POST whose Content-Type is not application/json gets 415 "
        "`unsupported_media_type`.",
        _unsupported_media_type,
    ),
    IntendedDifference(
        "malformed_json_parser_limit", "Phase 5 error contract (docs/api.md)",
        "A body FastAPI's JSON parser gave up on (e.g. nested too deeply) is 400 "
        "`malformed_json` instead of the generic `http_400`.",
        _parser_gave_up,
    ),
    IntendedDifference(
        "control_characters_rejected", "audit F50",
        "Control characters in query parameters are refused with 422 `validation_error` "
        "(the baseline let a NUL reach PostgreSQL: 500).",
        _control_characters,
    ),
    IntendedDifference(
        "lexicon_wording", "audit F23",
        "`activity_not_found` names the loaded release's lexicon as 'the <span> activity "
        "lexicon', and a malformed code (not 2, 4 or 6 digits) 'the activity lexicon' "
        "(release-neutral text); code and details unchanged.",
        _lexicon_wording,
    ),
    IntendedDifference(
        "health_reports_build", "Phase 5 contract (ATUS_BUILD_SHA)",
        "GET /health also reports `build`, which the deploy workflow polls.",
        _health_build,
    ),
)


def evaluate_pair(pair: str, left: Response, right: Response, request: RequestInfo, *,
                  expected_versions: dict[str, str] | None = None) -> PairResult:
    """Compare under the pair's rules; for ``a_b``, a difference one of
    :data:`INTENDED_DIFFERENCES` explains becomes ``intended_difference``. A
    data-version mismatch (wrong database) is never explained away."""
    result = compare_responses(pair, left, right, expected_versions=expected_versions)
    if result.outcome != "mismatch" or pair != "a_b":
        return result
    if any(m["path"] == "headers.x-data-version" for m in result.mismatches):
        return result
    for difference in INTENDED_DIFFERENCES:
        if difference.applies(left, right, request):
            result.outcome = "intended_difference"
            result.classification = difference.as_dict()
            break
    return result


# --------------------------------------------------------------------------- #
# one spec / one GET                                                           #
# --------------------------------------------------------------------------- #


def _status_of(responses: dict[str, Response], pairs: dict[str, PairResult]) -> str:
    """``incomplete`` when a target never answered (or only "not now"),
    ``server_error`` when a Phase 5 target answered 5xx (a baseline 5xx is
    a difference like any other: the a_b pair judges it), else the worst
    pair outcome."""
    if any(r.status is None or is_retryable(r) for r in responses.values()):
        return "incomplete"
    if any(r.status >= 500 for key, r in responses.items() if key != "a"):
        return "server_error"
    outcomes = {p.outcome for p in pairs.values()}
    if "mismatch" in outcomes:
        return "mismatch"
    if "intended_difference" in outcomes:
        return "intended_difference"
    return "equivalent"


def compare_all(responses: dict[str, Response], request: RequestInfo, *,
                expected_versions: dict[str, str] | None = None
                ) -> tuple[str, dict[str, PairResult]]:
    """The status of one request answered by the targets in ``responses``,
    and each pair's result (pairs whose targets were not run are skipped)."""
    pairs = {
        pair: evaluate_pair(pair, responses[left], responses[right], request,
                            expected_versions=expected_versions)
        for pair, (left, right) in PAIRS.items()
        if left in responses and right in responses
    }
    return _status_of(responses, pairs), pairs


def _target_summary(response: Response) -> dict:
    out: dict = {"status": response.status}
    if response.error_code:
        out["code"] = response.error_code
    if response.transport_error:
        out["transport_error"] = response.transport_error
    if response.attempts > 1:
        out["attempts"] = response.attempts
    return out


def evaluate_spec(spec: Spec, responses: dict[str, Response], *, max_body_bytes: int = 65_536,
                  expected_versions: dict[str, str] | None = None) -> dict:
    """The report entry for one spec."""
    request = spec.request(max_body_bytes)
    status, pairs = compare_all(responses, request, expected_versions=expected_versions)
    reference = responses.get("b") or next(iter(responses.values()))
    entry = {
        "id": spec.id,
        "source": spec.source,
        "endpoint": spec.endpoint,
        "category": spec.category,
        "expect": spec.expectation(),
        "expect_met": spec.expectation_met(reference),
        "status": status,
        "passed": status in PASSING,
        "targets": {key: _target_summary(r) for key, r in responses.items()},
        "pairs": {pair: result.as_dict() for pair, result in pairs.items()},
    }
    if spec.note:
        entry["note"] = spec.note
    return entry


def harness_error_entry(identity: dict, responses: dict[str, Response],
                        exc: BaseException) -> dict:
    """A failed report entry for a request the harness itself could not
    judge (a bug here, never a pass)."""
    return {
        **identity,
        "status": "incomplete",
        "passed": False,
        "harness_error": f"{type(exc).__name__}: {exc}"[:500],
        "targets": {key: _target_summary(r) for key, r in responses.items()},
        "pairs": {},
    }


def evaluate_get(path: str, responses: dict[str, Response], *,
                 expected_versions: dict[str, str] | None = None) -> dict:
    """The report entry for one GET: the same rules as a spec. ``b_c`` must
    be identical apart from the whitelist (full vs serving); ``a_b`` may
    differ only by an intended Phase 5 change (e.g. the F23 wording, the F50
    control-character refusal where the baseline answered 500)."""
    request = RequestInfo("GET", path)
    status, pairs = compare_all(responses, request, expected_versions=expected_versions)
    return {
        "path": path,
        "status": status,
        "passed": status in PASSING,
        "targets": {key: _target_summary(r) for key, r in responses.items()},
        "pairs": {pair: result.as_dict() for pair, result in pairs.items()},
    }


# --------------------------------------------------------------------------- #
# GET requests                                                                 #
# --------------------------------------------------------------------------- #

FIXED_GETS = (
    "/health",
    "/api/v1/meta",
    "/api/v1/activities",
    "/api/v1/activities/presets",
    "/api/v1/population/metadata",
    "/api/v1/activities?level=1",
    "/api/v1/activities?level=2",
    "/api/v1/activities?level=3",
    "/api/v1/activities?parent=01",
    "/api/v1/activities?parent=0101",
    "/api/v1/activities?parent=18&level=2",
    "/api/v1/activities?search=sleep",
    "/api/v1/activities?search=care",
    "/api/v1/activities?search=%25%25",               # LIKE metacharacters, escaped
    "/api/v1/activities?search=_l",
    "/api/v1/activities?search=caf%C3%A9",            # non-ASCII: the SQL ILIKE path
    "/api/v1/activities?search=x",                    # 422: shorter than 2
    "/api/v1/activities?level=7",                     # 422: out of range
    "/api/v1/activities?parent=1",                    # 422: pattern
    "/api/v1/activities?search=ab%00",                # control character (F50)
    "/api/v1/activities/99",                          # unknown codes (404)
    "/api/v1/activities/9999",
    "/api/v1/activities/999999",
    "/api/v1/activities/abc",                         # malformed codes (404)
    "/api/v1/activities/0101011",
    "/api/v1/no-such-route",                          # 404 not_found
)


def activity_detail_paths(listing: Any, *, sample: int, seed: int) -> list[str]:
    """``/api/v1/activities/<code>`` for every tier-1 code plus a seeded
    sample of ``sample`` tier-2 and 6-digit codes (half each), drawn from a
    ``GET /api/v1/activities`` body."""
    entries = listing.get("activities", []) if isinstance(listing, dict) else []
    by_level: dict[int, list[str]] = {1: [], 2: [], 3: []}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("level") in by_level:
            by_level[entry["level"]].append(str(entry["code"]))
    rng = random.Random(seed)
    tier2 = sorted(by_level[2])
    leaves = sorted(by_level[3])
    k2 = min(len(tier2), sample // 2)
    k3 = min(len(leaves), sample - k2)
    codes = sorted(by_level[1]) + sorted(rng.sample(tier2, k2)) + sorted(rng.sample(leaves, k3))
    return [f"/api/v1/activities/{code}" for code in codes]


# --------------------------------------------------------------------------- #
# report                                                                       #
# --------------------------------------------------------------------------- #


def summarize(specs: list[dict], gets: list[dict]) -> dict:
    """Totals for the report: counts by status, category and endpoint, the
    largest SE/CI differences per pair, intended and whitelisted differences."""
    by_status = Counter(s["status"] for s in specs)
    categories: dict[str, Counter] = {}
    endpoints: dict[str, Counter] = {}
    for s in specs:
        categories.setdefault(s["category"], Counter())[s["status"]] += 1
        endpoints.setdefault(s["endpoint"], Counter())[s["status"]] += 1
    pair_stats: dict[str, dict] = {}
    intended: Counter = Counter()
    whitelisted: Counter = Counter()
    for entry in [*specs, *gets]:
        for pair, result in entry["pairs"].items():
            stats = pair_stats.setdefault(pair, {
                "compared_requests": 0, "values_compared": 0, "outcomes": Counter(),
                "max_relative": 0.0, "max_relative_at": None,
                "max_scaled": 0.0, "max_scaled_at": None,
            })
            stats["compared_requests"] += 1
            stats["values_compared"] += result["compared"]
            stats["outcomes"][result["outcome"]] += 1
            name = entry.get("id", entry.get("path"))
            for metric in ("max_relative", "max_scaled"):
                value = math.inf if result[metric] == "inf" else result[metric]
                if value > stats[metric]:
                    stats[metric], stats[f"{metric}_at"] = value, name
            if result.get("classification"):
                intended[(pair, result["classification"]["id"])] += 1
            for note in result.get("whitelisted", []):
                whitelisted[(pair, note["rule"])] += 1
    for stats in pair_stats.values():
        stats["outcomes"] = dict(stats["outcomes"])
        for metric in ("max_relative", "max_scaled"):
            stats[metric] = _finite(stats[metric])
    failed_specs = [s["id"] for s in specs if not s["passed"]]
    failed_gets = [g["path"] for g in gets if not g["passed"]]
    return {
        "passed": not failed_specs and not failed_gets,
        "specs": len(specs),
        "specs_passed": len(specs) - len(failed_specs),
        "specs_failed": len(failed_specs),
        "by_status": {status: by_status.get(status, 0) for status in SPEC_STATUSES},
        "by_category": {k: dict(v) for k, v in sorted(categories.items())},
        "by_endpoint": {k: dict(v) for k, v in sorted(endpoints.items())},
        "expectation_misses": [s["id"] for s in specs if s["expect_met"] is False],
        "gets": len(gets),
        "gets_failed": failed_gets,
        "pairs": pair_stats,
        "intended_differences": [
            {"pair": pair, "id": ident, "count": count}
            for (pair, ident), count in sorted(intended.items())
        ],
        "whitelisted_differences": [
            {"pair": pair, "rule": rule, "count": count}
            for (pair, rule), count in sorted(whitelisted.items())
        ],
        "rel_tol": REL_TOL,
    }


def criterion_description() -> dict:
    return {
        "source": "atus_pipeline.serving.equivalence (compare_results, REL_TOL)",
        "rel_tol": REL_TOL,
        "tolerant_fields": sorted(TOLERANT_FIELDS),
        "rule": "|a - b| <= REL_TOL * max(|a|, |b|, S); S = the estimate's point value "
                "(for a comparison's difference also both groups' values); every other "
                "field exactly equal",
    }


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3g}"
    return str(value)


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _first_differences(entry: dict) -> str:
    """Why an entry failed, in one line: harness error, missing answers,
    then the first mismatches of each pair."""
    parts = []
    if entry.get("harness_error"):
        parts.append(f"harness error: {entry['harness_error']}")
    parts += [f"{key}: {target['transport_error']}"
              for key, target in entry["targets"].items() if target.get("transport_error")]
    parts += [f"{pair} {m['path']}: {m['detail']}"
              for pair, result in entry["pairs"].items() if result["outcome"] == "mismatch"
              for m in result.get("mismatches", [])[:2]]
    return "; ".join(parts)


def render_markdown(report: dict, *, max_failures: int = 50) -> str:
    """The human-readable summary of a report (``report.json``)."""
    summary = report["summary"]
    verdict = "PASS" if summary["passed"] else "FAIL"
    lines = [
        "# HTTP equivalence report",
        "",
        f"**{verdict}**: {summary['specs_passed']} of {summary['specs']} specs passed; "
        f"{summary['gets'] - len(summary['gets_failed'])} of {summary['gets']} GET requests "
        "passed.",
        "",
        f"Generated {report['generated_at']} by scripts/equivalence (harness version "
        f"{report['harness_version']}). Spec directory: `{report['inputs']['spec_dir']}` "
        f"({len(report['inputs']['spec_files'])} files).",
        "",
        "## Servers",
        "",
        "| Target | What | Code | Database | Data version |",
        "|---|---|---|---|---|",
    ]
    for key, server in report["servers"].items():
        lines.append(
            f"| {key} | {_cell(server['label'])} | `{server.get('commit', '?')}` | "
            f"{_cell(server.get('database', '?'))} | `{server.get('data_version', '?')}` |"
        )
    criterion = report["criterion"]
    lines += [
        "",
        "## Criterion",
        "",
        f"Standard errors and CI bounds (`{'`, `'.join(criterion['tolerant_fields'])}`): "
        f"{criterion['rule']} (REL_TOL = {criterion['rel_tol']:g}). Status codes identical; "
        "error bodies identical (b vs c); a-vs-b differences only where an intended Phase 5 "
        "change explains them exactly.",
        "",
        "## Results by status",
        "",
        "| Status | Specs |",
        "|---|---|",
    ]
    lines += [f"| {status} | {count} |" for status, count in summary["by_status"].items()]
    lines += ["", "## Pairs", "",
              "| Pair | Requests | Values compared | Outcomes | Max relative SE/CI diff | "
              "Max scaled diff (criterion ratio) |",
              "|---|---|---|---|---|---|"]
    for pair, stats in summary["pairs"].items():
        outcomes = ", ".join(f"{k} {v}" for k, v in sorted(stats["outcomes"].items()))
        lines.append(
            f"| {pair} ({PAIR_LABELS[pair]}) | {stats['compared_requests']} | "
            f"{stats['values_compared']:,} | {outcomes} | {_fmt(stats['max_relative'])} "
            f"({_cell(stats['max_relative_at'])}) | {_fmt(stats['max_scaled'])} "
            f"({_cell(stats['max_scaled_at'])}) |"
        )
    lines += ["", "The relative difference can be large on zero-variance estimates (noise "
              "around 0); the criterion bounds the scaled one.", "",
              "## By category", "", "| Category | " + " | ".join(SPEC_STATUSES) + " |",
              "|---|" + "---|" * len(SPEC_STATUSES)]
    for category, counts in summary["by_category"].items():
        lines.append(f"| {_cell(category)} | "
                     + " | ".join(str(counts.get(s, 0)) for s in SPEC_STATUSES) + " |")
    failures = [s for s in report["specs"] if not s["passed"]]
    lines += ["", f"## Failed specs ({len(failures)})", ""]
    if not failures:
        lines.append("None.")
    else:
        lines += ["| Spec | Status | Answers | First differences |", "|---|---|---|---|"]
        for spec in failures[:max_failures]:
            answers = ", ".join(f"{k}: {v['status']}{' ' + v['code'] if 'code' in v else ''}"
                                for k, v in spec["targets"].items())
            diffs = _first_differences(spec)
            lines.append(f"| `{_cell(spec['id'])}` ({_cell(spec['category'])}) | "
                         f"{spec['status']} | {_cell(answers)} | {_cell(diffs[:400])} |")
        if len(failures) > max_failures:
            lines.append(f"\n... and {len(failures) - max_failures} more (see report.json).")
    lines += ["", "## GET endpoints", "",
              "| Path | b vs c | a vs b | Answers |", "|---|---|---|---|"]
    for get in report["gets"]:
        b_c = get["pairs"].get("b_c", {}).get("outcome", "-")
        a_b_result = get["pairs"].get("a_b", {})
        a_b = a_b_result.get("outcome", "-")
        if a_b_result.get("classification"):
            a_b += f" ({a_b_result['classification']['id']})"
        answers = ", ".join(f"{k}: {v['status']}" for k, v in get["targets"].items())
        flag = "" if get["passed"] else " **FAIL**"
        lines.append(f"| `{_cell(get['path'])}`{flag} | {b_c} | {a_b} | {answers} |")
    failed_gets = [g for g in report["gets"] if not g["passed"]]
    if failed_gets:
        lines += ["", "Failed GET requests:", ""]
        for get in failed_gets:
            lines.append(f"- `{get['path']}` ({get['status']}): "
                         + _cell(_first_differences(get)[:400]))
    lines += ["", "## Intended Phase 5 differences (a vs b)", ""]
    if summary["intended_differences"]:
        counts = {d["id"]: d["count"] for d in summary["intended_differences"]}
        lines += ["| Difference | Source | Requests | What changed |", "|---|---|---|---|"]
        for difference in report["intended_differences"]:
            if difference["id"] in counts:
                lines.append(f"| `{difference['id']}` | {difference['source']} | "
                             f"{counts[difference['id']]} | {_cell(difference['description'])} |")
    else:
        lines.append("None observed.")
    lines += ["", "## Whitelisted fields", "", "| Field | Pairs | Differed in | Why |",
              "|---|---|---|---|"]
    seen = {(d["rule"]): d["count"] for d in summary["whitelisted_differences"]}
    for rule in report["whitelist"]["body"]:
        lines.append(f"| body key `{rule['key']}` | {', '.join(rule['pairs'])} | "
                     f"{seen.get(rule['id'], 0)} | {_cell(rule['justification'])} |")
    for name, why in report["whitelist"]["headers"].items():
        lines.append(f"| header `{name}` | all | (not compared) | {_cell(why)} |")
    compared = report["whitelist"].get("not_whitelisted", {})
    if compared:
        lines += ["", "Considered and deliberately **not** whitelisted (compared exactly):", ""]
        lines += [f"- {field}: {why}" for field, why in compared.items()]
    misses = summary["expectation_misses"]
    lines += ["", f"## Expectation misses ({len(misses)})", "",
              "Specs whose Phase 5 full-DB answer (b) was not what the spec expected "
              "(`ok` = 2xx). Not an equivalence failure"
              + (" (this run fails on them: --fail-on-unexpected)." if
                 report["inputs"].get("fail_on_unexpected") else ".")]
    if misses:
        lines.append("")
        lines += [f"- `{m}`" for m in misses[:100]]
    return "\n".join(lines) + "\n"


def report_passed(report: dict, *, fail_on_unexpected: bool = False) -> bool:
    summary = report["summary"]
    return summary["passed"] and not (fail_on_unexpected and summary["expectation_misses"])


def iter_targets(text: str) -> Iterable[str]:
    """``"abc"`` / ``"b,c"`` -> the targets, validated and in order."""
    chosen = {t for t in text.replace(",", "") if t.strip()}
    unknown = chosen - set(TARGETS)
    if unknown or len(chosen) < 2:
        raise ValueError(f"targets must be at least two of {', '.join(TARGETS)}")
    if not any(set(pair) <= chosen for pair in PAIRS.values()):
        raise ValueError("targets must include a pair: a+b or b+c")
    return [t for t in TARGETS if t in chosen]
