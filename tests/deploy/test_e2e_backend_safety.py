"""scripts/e2e_backend.py refuses non-disposable targets and never prints passwords (F60)."""

from __future__ import annotations

import importlib.util
import sys

import pytest

from .conftest import REPO_ROOT


@pytest.fixture(scope="module")
def e2e_backend():
    spec = importlib.util.spec_from_file_location(
        "e2e_backend", REPO_ROOT / "scripts" / "e2e_backend.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://atus:atus@localhost:5434/atus_e2e",  # the default
        "postgresql://atus:atus@localhost:5434/atus_e2e_wsd1",  # per-workstream names
        "postgresql://atus:atus@127.0.0.1:5434/atus_e2e_ci",
        "postgresql://atus@[::1]:5434/atus_e2e",
        "postgres://postgres:pw@db:5432/atus_e2e",  # a compose service name
        "postgresql:///atus_e2e?host=/var/run/postgresql",  # a Unix socket
    ],
)
def test_accepts_local_disposable_databases(e2e_backend, url):
    e2e_backend.check_disposable(url)


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("postgresql://atus:atus@localhost:5434/atus", "not named atus_e2e"),
        ("postgresql://atus:atus@localhost:5434/atus_test", "not named atus_e2e"),
        ("postgresql://atus:atus@localhost:5434/atus_e2e-prod", "not named atus_e2e"),
        ("postgresql://atus:atus@localhost:5434/my_atus_e2e", "not named atus_e2e"),
        ("postgresql://atus_api:pw@ep-cool-1.us-east-1.aws.neon.tech/atus_e2e", "not local"),
        ("postgresql://atus:atus@10.0.0.5:5432/atus_e2e", "not local"),
        ("postgresql://atus:atus@localhost/atus_e2e?host=db.example.com", "not local"),
        ("postgresql://atus:atus@localhost/atus_e2e?hostaddr=10.0.0.5", "not local"),
        ("postgresql://atus:atus@localhost/atus_e2e?dbname=atus", "not named atus_e2e"),
        ("postgresql://atus:atus@localhost/atus_e2e?service=prod", "service="),
        ("host=localhost dbname=atus_e2e", "postgresql:// URL"),
    ],
)
def test_refuses_anything_else(e2e_backend, url, reason):
    with pytest.raises(SystemExit) as excinfo:
        e2e_backend.check_disposable(url)
    message = str(excinfo.value)
    assert reason in message
    assert "refusing to drop schema atus" in message


def test_refusal_never_contains_the_password(e2e_backend):
    secret = "s3cr3t-Neon-pw"
    with pytest.raises(SystemExit) as excinfo:
        e2e_backend.check_disposable(f"postgresql://atus_api:{secret}@ep-x.neon.tech/atus")
    assert secret not in str(excinfo.value)
    assert "atus_api:***@ep-x.neon.tech" in str(excinfo.value)


@pytest.mark.parametrize(
    ("url", "redacted"),
    [
        ("postgresql://atus:atus@localhost:5434/atus_e2e",
         "postgresql://atus:***@localhost:5434/atus_e2e"),
        ("postgresql://u:p%40ss@h/db?sslmode=require", "postgresql://u:***@h/db?sslmode=require"),
        ("postgresql://u@h/db", "postgresql://u@h/db"),
        ("postgresql:///atus_e2e", "postgresql:///atus_e2e"),
    ],
)
def test_redact_url(e2e_backend, url, redacted):
    assert e2e_backend.redact_url(url) == redacted
