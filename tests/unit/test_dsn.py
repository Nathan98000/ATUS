"""Connection strings never leak: redaction and libpq environment hand-off."""

from __future__ import annotations

import pytest

from atus_pipeline.database.dsn import describe, libpq_env, redact, same_database


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
