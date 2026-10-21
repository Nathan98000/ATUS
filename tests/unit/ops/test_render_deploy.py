"""scripts/ops/render_deploy.py: the deploy hook by digest and the wait for the build.

The hook is never called for real: ``_open`` is replaced, and the origin is a
local fake server. Both inputs are secrets, so every test also checks that
neither appears in the output.
"""

from __future__ import annotations

import io
import json
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

from .conftest import OPS_DIR, json_reply, load_script

render = load_script(OPS_DIR / "render_deploy.py", "atus_ops_render_deploy")

DIGEST = "sha256:" + "ab" * 32
IMAGE = f"ghcr.io/nathan98000/atus-api@{DIGEST}"
HOOK_KEY = "hookkey-0123456789abcdefSECRET"
HOOK = f"https://api.render.com/deploy/srv-abc123def456?key={HOOK_KEY}"
SHA = "0123456789abcdef0123456789abcdef01234567"


class FakeResponse(io.BytesIO):
    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def hook_calls(monkeypatch):
    calls: list[dict] = []
    replies: list = []

    def fake_open(request, timeout):
        calls.append({"url": request.full_url, "method": request.get_method(),
                      "data": request.data})
        reply = replies.pop(0) if replies else FakeResponse(
            200, json.dumps({"deploy": {"id": "dep-cd5k3x9q2abc"}}).encode())
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(render, "_open", fake_open)
    return calls, replies


def test_the_image_reference_must_be_a_digest_in_the_service_repository():
    assert render.validate_image_ref(IMAGE, render.DEFAULT_IMAGE_REPO) == IMAGE
    for bad in ("ghcr.io/nathan98000/atus-api:latest",
                "ghcr.io/nathan98000/atus-api@sha256:abc",
                f"ghcr.io/someone/else@{DIGEST}",
                f"docker.io/library/atus-api@{DIGEST}"):
        with pytest.raises(render.OpsError):
            render.validate_image_ref(bad, render.DEFAULT_IMAGE_REPO)


def test_the_hook_url_gets_the_url_encoded_image_reference():
    url = render.hook_request_url(HOOK, IMAGE)
    query = parse_qs(urlsplit(url).query)
    assert query["key"] == [HOOK_KEY]
    assert query["imgURL"] == [IMAGE]
    assert "imgURL=ghcr.io%2Fnathan98000%2Fatus-api%40sha256%3A" in url


@pytest.mark.parametrize("hook", [
    "http://api.render.com/deploy/srv-x?key=k",            # not https
    "https://evil.example/deploy/srv-x?key=k",             # another host
    "https://api.render.com/other/srv-x?key=k",            # not a deploy hook
    "https://api.render.com/deploy/srv-x",                 # no key
    "https://api.render.com/deploy/srv-x?key=k&imgURL=x",  # already has an image
])
def test_unexpected_hook_urls_are_refused_without_echoing_them(hook):
    with pytest.raises(render.OpsError) as info:
        render.hook_request_url(hook, IMAGE)
    assert "key=k" not in str(info.value)


def test_trigger_posts_once_and_reports_the_deploy_id(hook_calls, monkeypatch, capsys,
                                                      tmp_path):
    calls, _ = hook_calls
    output = tmp_path / "out"
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert render.main(["trigger", "--image-ref", IMAGE]) == 0
    (call,) = calls
    assert call["method"] == "POST" and call["data"] == b""
    assert parse_qs(urlsplit(call["url"]).query)["imgURL"] == [IMAGE]
    out = capsys.readouterr()
    assert "dep-cd5k3x9q2abc" in out.out
    assert HOOK_KEY not in out.out + out.err
    assert "deploy_id=dep-cd5k3x9q2abc" in output.read_text()


def test_a_refused_hook_reports_only_the_status(hook_calls, monkeypatch, capsys):
    _, replies = hook_calls
    replies.append(urllib.error.HTTPError(HOOK, 404, "Not Found", {}, io.BytesIO(HOOK.encode())))
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK)
    assert render.main(["trigger", "--image-ref", IMAGE]) == 1
    out = capsys.readouterr()
    assert "HTTP 404" in out.err
    assert HOOK_KEY not in out.out + out.err and "srv-abc123" not in out.out + out.err


def test_an_unreachable_hook_does_not_leak_the_url(hook_calls, monkeypatch, capsys):
    _, replies = hook_calls
    replies.append(urllib.error.URLError(f"cannot reach {HOOK}"))
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK)
    assert render.main(["trigger", "--image-ref", IMAGE]) == 1
    out = capsys.readouterr()
    assert HOOK_KEY not in out.out + out.err


def test_trigger_needs_the_hook_secret(hook_calls, capsys):
    calls, _ = hook_calls
    assert render.main(["trigger", "--image-ref", IMAGE]) == 1
    assert calls == [] and "RENDER_DEPLOY_HOOK_URL" in capsys.readouterr().err


def test_a_tag_is_refused_before_anything_is_sent(hook_calls, monkeypatch):
    calls, _ = hook_calls
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK)
    assert render.main(["trigger", "--image-ref", "ghcr.io/nathan98000/atus-api:v1.0.0"]) == 1
    assert calls == []


def test_wait_polls_until_the_origin_reports_the_build(fake_server, monkeypatch, capsys):
    replies = iter([json_reply(200, {"status": "ok", "build": "old0000"}),
                    (503, {"Content-Type": "text/html"}, b"<html>waking</html>"),
                    json_reply(200, {"status": "ok", "build": SHA})])
    fake_server.routes[("GET", "/health")] = lambda _r: next(replies)
    monkeypatch.setenv("RENDER_ORIGIN", fake_server.base)
    assert render.main(["wait", "--expect-build", SHA, "--timeout", "60",
                        "--interval", "5"]) == 0
    assert fake_server.count("GET", "/health") == 3
    out = capsys.readouterr()
    assert f"origin runs build {SHA}" in out.out
    assert fake_server.base not in out.out + out.err  # the origin URL is not published


def test_wait_warns_when_the_origin_already_ran_this_build(fake_server, monkeypatch, capsys):
    # A re-run rebuilt and pushed a new digest of the same commit: /health
    # (which reports the commit) cannot show the switch, so say so.
    fake_server.routes[("GET", "/health")] = json_reply(200, {"status": "ok", "build": SHA})
    monkeypatch.setenv("RENDER_ORIGIN", fake_server.base)
    assert render.main(["wait", "--expect-build", SHA, "--previous-build", SHA,
                        "--timeout", "60", "--interval", "5"]) == 0
    out = capsys.readouterr().out
    assert "::warning title=Render deploy not observed::" in out
    assert fake_server.base not in out


@pytest.mark.parametrize("previous", ["", "unknown", "old0000"])
def test_wait_does_not_warn_for_another_or_unknown_previous_build(fake_server, monkeypatch,
                                                                  capsys, previous):
    fake_server.routes[("GET", "/health")] = json_reply(200, {"status": "ok", "build": SHA})
    monkeypatch.setenv("RENDER_ORIGIN", fake_server.base)
    assert render.main(["wait", "--expect-build", SHA, "--previous-build", previous,
                        "--timeout", "60", "--interval", "5"]) == 0
    assert "::warning" not in capsys.readouterr().out


def test_wait_gives_up_after_its_budget(fake_server, monkeypatch, capsys):
    fake_server.routes[("GET", "/health")] = json_reply(200, {"status": "ok", "build": "old"})
    monkeypatch.setenv("RENDER_ORIGIN", fake_server.base)
    assert render.main(["wait", "--expect-build", SHA, "--timeout", "30",
                        "--interval", "10"]) == 1
    # ceil(30 / 10) + 2 requests at most
    assert fake_server.count() <= 5
    assert "not ready" in capsys.readouterr().err


def test_current_build_is_written_as_a_step_output(fake_server, monkeypatch, tmp_path):
    fake_server.routes[("GET", "/health")] = json_reply(200, {"status": "ok", "build": SHA})
    output = tmp_path / "out"
    monkeypatch.setenv("RENDER_ORIGIN", fake_server.base)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert render.main(["current-build"]) == 0
    assert output.read_text() == f"build={SHA}\n"


def test_malformed_builds_are_not_trusted(fake_server, monkeypatch, tmp_path):
    fake_server.routes[("GET", "/health")] = json_reply(200, {"status": "ok",
                                                              "build": "a b; rm -rf /"})
    monkeypatch.setenv("RENDER_ORIGIN", fake_server.base)
    assert render.main(["current-build", "--wake-timeout", "1"]) == 1
