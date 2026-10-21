#!/usr/bin/env python3
"""Deploy the API image to Render by digest and wait for it (ADR-011 "Deploy").

Used by ``.github/workflows/deploy.yml``; standard library only. Both inputs
are secrets and are read from the environment, never from the command line,
and never printed (messages say "the deploy hook" and "origin"):

    RENDER_DEPLOY_HOOK_URL   https://api.render.com/deploy/srv-<id>?key=<key>
    RENDER_ORIGIN            https://<service>.onrender.com (never published)

Commands:

    render_deploy.py current-build
        GET origin /health once the instance is awake; prints the build
        (git SHA) it runs, for the rollback notes. /health never touches the DB.

    render_deploy.py trigger --image-ref ghcr.io/nathan98000/atus-api@sha256:<hex>
        POST the deploy hook with imgURL=<the URL-encoded reference>. Render
        requires every part but the digest to match the service's image URL.

    render_deploy.py wait --expect-build <git sha> [--expect-data-version <dv>]
                          [--previous-build <sha>] [--timeout 900]
        Poll origin /health until it reports that build: the image is pulled,
        started and routed. With --expect-data-version, the origin must then
        also be bound to that data version (/health's
        ``expected_data_version``, Render's ATUS_EXPECTED_DATA_VERSION), or the
        wait fails at once: deploy.yml publishes the edge's release vars and
        snapshot only after this, so a deploy approved before the release
        switch on Render publishes nothing at the edge (review round 2,
        no-prepublish-data-version-check). Allows for Render's ~1 min spin-up plus the app's
        start (~13 s at 0.1 CPU in the 2026-10-21 emulation; F67) and the
        image pull. /health reports the commit, not
        the digest: when the origin already ran this commit before the hook
        (``--previous-build``, e.g. a re-run that rebuilt and pushed a new
        digest), the wait cannot see the switch, so it says so as a GitHub
        warning instead of implying the new digest was observed.

Exit status: 0 success, 1 failure, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from httpcheck import Client, NoRedirect, OpsError, ceil_div  # noqa: E402

DEFAULT_IMAGE_REPO = "ghcr.io/nathan98000/atus-api"
IMAGE_REF = re.compile(r"^(?P<repo>[a-z0-9][a-z0-9._/-]*)@sha256:[0-9a-f]{64}$")
BUILD = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
DEPLOY_ID = re.compile(r"^dep-[a-z0-9]{8,64}$")
HOOK_HOST = "api.render.com"
_OPENER = urllib.request.build_opener(NoRedirect)


def _open(request: urllib.request.Request, timeout: float):
    """Send without following redirects (a redirect could carry the key elsewhere)."""
    return _OPENER.open(request, timeout=timeout)


def validate_image_ref(image_ref: str, image_repo: str) -> str:
    match = IMAGE_REF.match(image_ref)
    if not match:
        raise OpsError("--image-ref must be <repository>@sha256:<64 hex> (a digest, never a tag)")
    if match.group("repo") != image_repo:
        raise OpsError(f"--image-ref must be in {image_repo}; Render refuses any other "
                       "repository than the service's image URL")
    return image_ref


def hook_request_url(hook_url: str, image_ref: str) -> str:
    """The deploy-hook URL with ``imgURL`` appended (validated; never printed)."""
    try:
        parts = urlsplit(hook_url.strip())
    except ValueError as exc:
        raise OpsError("RENDER_DEPLOY_HOOK_URL is not a URL") from exc
    if parts.scheme != "https" or parts.hostname != HOOK_HOST:
        raise OpsError(f"RENDER_DEPLOY_HOOK_URL must be an https://{HOOK_HOST}/deploy/... URL")
    if not parts.path.startswith("/deploy/srv-") or "key=" not in parts.query:
        raise OpsError("RENDER_DEPLOY_HOOK_URL must look like "
                       f"https://{HOOK_HOST}/deploy/srv-<id>?key=<key>")
    if parts.username or parts.password or parts.fragment or "imgURL=" in parts.query:
        raise OpsError("RENDER_DEPLOY_HOOK_URL has unexpected parts")
    return f"{hook_url.strip()}&imgURL={quote(image_ref, safe='')}"


def trigger(hook_url: str, image_ref: str, *, timeout: float = 60.0) -> str | None:
    """POST the deploy hook; returns Render's deploy id when it reports one."""
    url = hook_request_url(hook_url, image_ref)
    request = urllib.request.Request(url, data=b"", method="POST",
                                     headers={"User-Agent": "atus-deploy/1"})
    try:
        with _open(request, timeout) as response:
            status, body = response.status, response.read(65_536)
    except urllib.error.HTTPError as exc:
        # Only the status: the reply could quote the request.
        raise OpsError(f"the deploy hook refused the request: HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise OpsError(f"the deploy hook could not be reached ({type(reason).__name__})") \
            from None
    if status not in (200, 201, 202):
        raise OpsError(f"the deploy hook answered HTTP {status}")
    try:
        deploy = json.loads(body).get("deploy", {})
        deploy_id = deploy.get("id") if isinstance(deploy, dict) else None
    except (ValueError, AttributeError):
        deploy_id = None
    return deploy_id if isinstance(deploy_id, str) and DEPLOY_ID.match(deploy_id) else None


def _build_of(reply) -> str | None:
    if reply.status != 200 or not reply.is_json:
        return None
    try:
        build = reply.json().get("build")
    except (OpsError, AttributeError):
        return None
    return build if isinstance(build, str) and BUILD.match(build) else None


def check_data_binding(reply, expected: str) -> None:
    """The origin is bound to ``expected``: ``reply`` is the /health answer that
    reported the build, and its ``expected_data_version`` is Render's
    ATUS_EXPECTED_DATA_VERSION (from the configuration, not the database)."""
    bound = None
    if reply.status == 200 and reply.is_json:
        try:
            bound = reply.json().get("expected_data_version")
        except (OpsError, AttributeError):
            bound = None
    if not isinstance(bound, str) or not bound:
        raise OpsError(f"the origin is not bound to a data version ({reply.describe()}): set "
                       f"Render's ATUS_EXPECTED_DATA_VERSION to {expected} together with "
                       "ATUS_DATABASE_URL; nothing was published at the edge")
    if bound != expected:
        raise OpsError(f"the origin is bound to data version {bound[:200]} "
                       f"(ATUS_EXPECTED_DATA_VERSION), but this deploy publishes {expected} "
                       "(releases/current.json): switch Render to the new release first "
                       "(docs/operations.md, Data release step 6), then re-run this job; "
                       "nothing was published at the edge")


def current_build(origin: Client, *, deadline_s: float) -> str:
    reply = origin.wait_for("GET", "/health", lambda r: _build_of(r) is not None,
                            deadline_s=deadline_s, min_interval_s=15.0,
                            max_attempts=origin.remaining, timeout=90.0)
    return _build_of(reply) or "unknown"


def wait_for_build(origin: Client, expected: str, *, deadline_s: float,
                   interval_s: float):
    """The first /health reply that reports ``expected``."""
    seen: list[str | None] = []

    def ready(reply) -> bool:
        build = _build_of(reply)
        if not seen or seen[-1] != build:
            print(f"origin /health reports build {build or 'none'} "
                  f"({reply.describe()})", flush=True)
            seen.append(build)
        return build == expected

    return origin.wait_for("GET", "/health", ready, deadline_s=deadline_s,
                           min_interval_s=interval_s, max_attempts=origin.remaining,
                           timeout=60.0)


def _origin(max_requests: int) -> Client:
    url = os.environ.get("RENDER_ORIGIN", "").strip()
    if not url:
        raise OpsError("set RENDER_ORIGIN (the Render service URL)")
    return Client(url, label="origin", max_requests=max_requests, timeout=60.0)


def _write_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cur = sub.add_parser("current-build", help="the build the origin runs now")
    cur.add_argument("--wake-timeout", type=float, default=180.0)
    trig = sub.add_parser("trigger", help="call the deploy hook with an image digest")
    trig.add_argument("--image-ref", required=True)
    trig.add_argument("--image-repo", default=DEFAULT_IMAGE_REPO)
    wait = sub.add_parser("wait", help="poll origin /health until it reports a build")
    wait.add_argument("--expect-build", required=True)
    wait.add_argument("--expect-data-version", default=None,
                      help="the data version the origin must be bound to (the release's)")
    wait.add_argument("--previous-build", default="",
                      help="the build the origin ran before the hook (current-build)")
    wait.add_argument("--timeout", type=float, default=900.0)
    wait.add_argument("--interval", type=float, default=15.0)
    args = parser.parse_args(argv)

    try:
        if args.command == "current-build":
            build = current_build(_origin(8), deadline_s=args.wake_timeout)
            print(f"origin runs build {build}")
            _write_output("build", build)
        elif args.command == "trigger":
            image_ref = validate_image_ref(args.image_ref, args.image_repo)
            hook = os.environ.get("RENDER_DEPLOY_HOOK_URL", "")
            if not hook.strip():
                raise OpsError("set RENDER_DEPLOY_HOOK_URL (the service's deploy hook)")
            deploy_id = trigger(hook, image_ref)
            print(f"deploy hook accepted {image_ref}" +
                  (f" (Render deploy {deploy_id})" if deploy_id else ""))
            _write_output("deploy_id", deploy_id or "")
        else:
            if not BUILD.match(args.expect_build):
                raise OpsError("--expect-build must be 1-64 characters of [A-Za-z0-9._-]")
            if not 1 <= args.interval <= 120 or not 10 <= args.timeout <= 3600:
                raise OpsError("--interval must be 1-120 s and --timeout 10-3600 s")
            if args.previous_build.strip() == args.expect_build:
                print(f"::warning title=Render deploy not observed::the origin already ran "
                      f"build {args.expect_build} before the deploy hook, and /health reports "
                      "the commit, not the image digest, so this wait cannot show when the "
                      "new digest is live. Check Render -> Events for this deploy.", flush=True)
            budget = ceil_div(args.timeout, args.interval) + 2
            reply = wait_for_build(_origin(budget), args.expect_build,
                                   deadline_s=args.timeout, interval_s=args.interval)
            print(f"origin runs build {args.expect_build}")
            if args.expect_data_version:
                check_data_binding(reply, args.expect_data_version)
                print(f"origin is bound to data version {args.expect_data_version}")
    except OpsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
