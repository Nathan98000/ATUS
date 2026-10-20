"""Connection strings: redaction, and handing them to libpq tools safely.

Connection strings carry passwords, so they never appear in output, logs,
reports or another process's argument list (brief §19):

* :func:`redact` masks the password of every connection string inside a
  piece of text (URL form ``postgresql://user:secret@host/db`` and
  key-value form ``password=secret``), for anything that is printed or saved;
* :func:`libpq_env` turns a connection string into libpq environment
  variables (``PGHOST``, ``PGPASSWORD``, ...), so command-line tools such as
  ``pg_dump`` and ``psql`` get the password through their environment rather
  than their arguments, where ``ps`` would show it.
"""

from __future__ import annotations

import re

from psycopg.conninfo import conninfo_to_dict

MASK = "***"

# postgresql://user:password@host  ->  postgresql://user:***@host
_URL_PASSWORD = re.compile(r"(?P<head>\bpostgres(?:ql)?://[^\s:/@]*:)(?P<secret>[^\s@]*)(?=@)")
# password=secret / password='se cret' in key-value connection strings
_KV_PASSWORD = re.compile(
    r"(?P<head>(?<![?&\w])password\s*=\s*)(?P<secret>'(?:[^'\\]|\\.)*'|[^\s']+)",
    re.IGNORECASE,
)
# ?password=secret / &password=secret in URL query strings
_QUERY_PASSWORD = re.compile(r"(?P<head>[?&]password=)(?P<secret>[^&\s]*)", re.IGNORECASE)

# libpq connection keywords -> the environment variables libpq reads them from
# (https://www.postgresql.org/docs/16/libpq-envars.html).
_ENV_VARS = {
    "host": "PGHOST",
    "hostaddr": "PGHOSTADDR",
    "port": "PGPORT",
    "dbname": "PGDATABASE",
    "user": "PGUSER",
    "password": "PGPASSWORD",
    "options": "PGOPTIONS",
    "application_name": "PGAPPNAME",
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "sslcrl": "PGSSLCRL",
    "sslnegotiation": "PGSSLNEGOTIATION",
    "requiressl": "PGREQUIRESSL",
    "channel_binding": "PGCHANNELBINDING",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
    "gssencmode": "PGGSSENCMODE",
    "sslsni": "PGSSLSNI",
    "require_auth": "PGREQUIREAUTH",
    "ssl_min_protocol_version": "PGSSLMINPROTOCOLVERSION",
    "ssl_max_protocol_version": "PGSSLMAXPROTOCOLVERSION",
}


def redact(text: str, *secrets: str) -> str:
    """``text`` with every connection-string password, and every literal in
    ``secrets``, replaced by ``***``."""
    redacted = _URL_PASSWORD.sub(lambda m: m["head"] + MASK, text)
    redacted = _KV_PASSWORD.sub(lambda m: m["head"] + MASK, redacted)
    redacted = _QUERY_PASSWORD.sub(lambda m: m["head"] + MASK, redacted)
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, MASK)
    return redacted


def describe(conninfo: str) -> str:
    """A printable name for a database: ``user@host:port/dbname``, no password."""
    params = conninfo_to_dict(conninfo)
    host = params.get("host") or "localhost"
    port = params.get("port") or "5432"
    return f"{params.get('user', '')}@{host}:{port}/{params.get('dbname', '')}"


def libpq_env(conninfo: str) -> dict[str, str]:
    """libpq environment variables equivalent to ``conninfo``.

    Raises ``ValueError`` for a parameter libpq cannot take from the
    environment, rather than silently dropping it.
    """
    env: dict[str, str] = {}
    for key, value in conninfo_to_dict(conninfo).items():
        if value is None or value == "":
            continue
        if key not in _ENV_VARS:
            raise ValueError(f"connection parameter {key!r} has no libpq environment variable")
        env[_ENV_VARS[key]] = str(value)
    return env


def same_database(a: str, b: str) -> bool:
    """Whether two connection strings name the same database on the same server."""
    pa, pb = conninfo_to_dict(a), conninfo_to_dict(b)

    def where(p: dict) -> tuple:
        return (p.get("host") or "localhost", str(p.get("port") or "5432"), p.get("dbname"))

    loopback = {"localhost", "127.0.0.1", "::1"}
    wa, wb = where(pa), where(pb)
    if wa[0] in loopback and wb[0] in loopback:
        return wa[1:] == wb[1:]
    return wa == wb
