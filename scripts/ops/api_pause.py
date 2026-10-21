#!/usr/bin/env python3
"""The owner's pause switch for the API at the edge (``edge/src/pause.ts``).

What a suspended Render service answers is undocumented, so the Worker cannot
detect a suspension (docs/security.md R13). When the owner knows the API is
paused, ``.github/workflows/deploy.yml`` (operations ``pause-api`` and
``resume-api``) sets or clears two Worker secrets with ``wrangler secret
bulk``: one new Worker version with the same code, site and snapshot. Every
request that would reach Render then gets 503 ``api_paused`` with the date,
and the SPA shows its paused page. Standard library only:

    api_pause.py secrets --operation pause-api --until 2026-12-01 \\
        --reason render_quota --out pause.json
        Check the inputs and write the JSON ``wrangler secret bulk`` takes:
        ``API_PAUSED_UNTIL`` (a future UTC date ``YYYY-MM-DD`` or time
        ``YYYY-MM-DDTHH:MM[:SS]Z``, or ``unknown``) and ``API_PAUSED_REASON``
        (``render_quota``, ``render_suspended`` or ``maintenance``). For
        ``resume-api`` both are null, which deletes them.

    api_pause.py verify --base-url https://atus.<sub>.workers.dev \\
        --operation pause-api --until 2026-12-01 --reason render_quota
        Read ``/api/_edge/health`` (answered by the Worker; nothing reaches
        Render or Neon) until its ``api_paused`` field shows the new state:
        at most 6 requests, about a minute.

Exit status: 0 success, 1 failure, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from httpcheck import Client, OpsError  # noqa: E402

OPERATIONS = ("pause-api", "resume-api")
REASONS = ("render_quota", "render_suspended", "maintenance")
#: As edge/src/pause.ts reads it.
UNTIL = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2}))?Z)?$")
VERIFY_REQUESTS = 6


def parse_until(text: str) -> datetime | None:
    """A switch time as UTC, or None for ``unknown``; raises OpsError otherwise."""
    if text.strip().lower() == "unknown":
        return None
    match = UNTIL.match(text.strip())
    if match is None:
        raise OpsError("--until must be YYYY-MM-DD, YYYY-MM-DDTHH:MM[:SS]Z (UTC) or 'unknown'")
    parts = [int(part) if part else 0 for part in match.groups()]
    try:
        return datetime(*parts, tzinfo=UTC)  # type: ignore[misc]
    except ValueError:
        raise OpsError("--until names a date or time that does not exist") from None


def iso(moment: datetime) -> str:
    """As the Worker reports it (JavaScript's toISOString)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def plan(operation: str, until: str, reason: str, *, now: datetime) -> tuple[dict, dict | None]:
    """The secrets for ``wrangler secret bulk`` and the ``api_paused`` the edge should report."""
    if operation not in OPERATIONS:
        raise OpsError(f"--operation must be one of {', '.join(OPERATIONS)}")
    if operation == "resume-api":
        return {"API_PAUSED_UNTIL": None, "API_PAUSED_REASON": None}, None
    if reason not in REASONS:
        raise OpsError(f"--reason must be one of {', '.join(REASONS)}")
    moment = parse_until(until)
    if moment is not None and moment <= now:
        raise OpsError("--until is already past: the switch would be off at once")
    value = "unknown" if moment is None else until.strip()
    expected = {"until": None if moment is None else iso(moment), "reason": reason}
    return {"API_PAUSED_UNTIL": value, "API_PAUSED_REASON": reason}, expected


def verify(client: Client, expected: dict | None) -> dict | None:
    """Poll /api/_edge/health until ``api_paused`` equals ``expected``; return it."""

    def shows(reply) -> bool:
        if reply.status != 200 or not reply.is_json:
            return False
        try:
            body = reply.json()
        except OpsError:
            return False
        return isinstance(body, dict) and "api_paused" in body and \
            body["api_paused"] == expected

    reply = client.wait_for("GET", "/api/_edge/health", shows, deadline_s=90.0,
                            min_interval_s=15.0, max_attempts=client.remaining)
    return reply.json()["api_paused"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("secrets", "verify"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--operation", required=True)
        cmd.add_argument("--until", default="")
        cmd.add_argument("--reason", default="")
        if name == "secrets":
            cmd.add_argument("--out", required=True, type=Path)
        else:
            cmd.add_argument("--base-url", default=os.environ.get("PUBLIC_BASE_URL"))
    args = parser.parse_args(argv)

    try:
        secrets, expected = plan(args.operation, args.until, args.reason,
                                 now=datetime.now(UTC))
        if args.command == "secrets":
            args.out.write_text(json.dumps(secrets) + "\n", encoding="utf-8")
            state = "off" if expected is None else json.dumps(expected)
            print(f"wrote {len(secrets)} secret changes for {args.operation}; the edge should "
                  f"report api_paused = {state}")
            return 0
        if not args.base_url:
            print("error: pass --base-url or set PUBLIC_BASE_URL", file=sys.stderr)
            return 2
        shown = verify(Client(args.base_url, label="site", max_requests=VERIFY_REQUESTS),
                       expected)
        print("the edge reports the switch " +
              ("off" if shown is None else f"on: {json.dumps(shown)}"))
    except OpsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
