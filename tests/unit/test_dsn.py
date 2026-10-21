"""Connection strings never leak: redaction and libpq environment hand-off."""

from __future__ import annotations

import pytest

from atus_pipeline.database import dsn
from atus_pipeline.database.dsn import (
    describe,
    ensure_system_roots,
    libpq_env,
    redact,
    same_database,
)


@pytest.mark.parametrize("text, expected", [
    ("postgresql://neondb_owner:s3cr%40t@ep-x.us-east-1.aws.neon.tech/atus?sslmode=require",
     "postgresql://neondb_owner:***@ep-x.us-east-1.aws.neon.tech/atus?sslmode=require"),
    ("failed: postgres://u:pw@h:5432/db and postgresql://v:pw2@k/db",
     "failed: postgres://u:***@h:5432/db and postgresql://v:***@k/db"),
    ("host=h user=u password=secret dbname=d", "host=h user=u password=*** dbname=d"),
    ("host=h password='se cret' dbname=d", "host=h password=*** dbname=d"),
    ("postgresql://h/db?user=u&password=pw&sslmode=require",
     "postgresql://h/db?user=u&password=***&sslmode=require"),
    ("postgresql://u@h/db (no password)", "postgresql://u@h/db (no password)"),
])
def test_redact_masks_every_connection_string_password(text, expected):
    assert redact(text) == expected


def test_redact_masks_known_secrets_anywhere():
    assert redact("token abc123 in the middle", "abc123", "") == "token *** in the middle"


def test_describe_names_the_database_without_the_password():
    described = describe("postgresql://owner:hunter2@db.example:6543/atus")
    assert described == "owner@db.example:6543/atus"
    assert "hunter2" not in described


def test_libpq_env_carries_the_password_in_the_environment():
    env = libpq_env(
        "postgresql://owner:p%40ss@ep-1.neon.tech:5432/atus?sslmode=require&channel_binding=require"
    )
    assert env == {
        "PGHOST": "ep-1.neon.tech", "PGPORT": "5432", "PGUSER": "owner", "PGPASSWORD": "p@ss",
        "PGDATABASE": "atus", "PGSSLMODE": "require", "PGCHANNELBINDING": "require",
    }


def test_libpq_env_refuses_a_parameter_it_cannot_pass():
    with pytest.raises(ValueError, match="keepalives"):
        libpq_env("postgresql://u@h/db?keepalives=1")


def test_same_database_treats_loopback_names_alike():
    assert same_database("postgresql://a@localhost:5434/atus", "postgresql://b@127.0.0.1:5434/atus")
    assert not same_database("postgresql://a@localhost:5434/atus",
                             "postgresql://a@localhost:5465/atus")
    assert not same_database("postgresql://a@h/atus", "postgresql://a@h/atus_serving")


NEON = "postgresql://atus_api:pw@ep-1.us-east-1.aws.neon.tech/atus"


def test_sslrootcert_system_gets_the_operating_systems_ca_bundle(tmp_path, monkeypatch):
    # The psycopg[binary] wheel's OpenSSL looks for its roots under a build
    # path (/tmp/libpq.build) that does not exist on the owner's Mac, so
    # `system` alone finds none: the bundle must come from SSL_CERT_FILE.
    bundle = tmp_path / "cert.pem"
    bundle.write_text("roots")
    monkeypatch.setattr(dsn, "SYSTEM_CA_BUNDLES", (str(tmp_path / "absent.pem"), str(bundle)))
    environ: dict[str, str] = {}
    assert ensure_system_roots(f"{NEON}?sslmode=verify-full&sslrootcert=system",
                               environ=environ) == str(bundle)
    assert environ == {"SSL_CERT_FILE": str(bundle)}


def test_an_explicit_ssl_cert_file_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(dsn, "SYSTEM_CA_BUNDLES", (str(tmp_path),))
    environ = {"SSL_CERT_FILE": "/owner/choice.pem"}
    assert ensure_system_roots(f"{NEON}?sslrootcert=system", environ=environ) == \
        "/owner/choice.pem"
    assert environ == {"SSL_CERT_FILE": "/owner/choice.pem"}


def test_connections_without_sslrootcert_system_leave_the_environment_alone(tmp_path,
                                                                           monkeypatch):
    bundle = tmp_path / "cert.pem"
    bundle.write_text("roots")
    monkeypatch.setattr(dsn, "SYSTEM_CA_BUNDLES", (str(bundle),))
    environ: dict[str, str] = {}
    for url in (f"{NEON}?sslmode=require", "postgresql://atus:atus@localhost:5434/atus",
                f"{NEON}?sslmode=verify-full&sslrootcert=/path/ca.pem"):
        assert ensure_system_roots(url, environ=environ) is None
    assert environ == {}


def test_without_any_bundle_nothing_is_set(tmp_path, monkeypatch):
    monkeypatch.setattr(dsn, "SYSTEM_CA_BUNDLES", (str(tmp_path / "absent.pem"),))
    environ: dict[str, str] = {}
    assert ensure_system_roots(f"{NEON}?sslrootcert=system", environ=environ) is None
    assert environ == {}
