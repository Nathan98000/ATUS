"""The owner's secrets file and the API role's password (brief §19).

Provider secrets live in a gitignored file outside the repository that the
owner fills in, by default ``~/.config/atus/neon.env``: ``KEY=value`` lines
(``#`` comments allowed). The restore reads the owner's connection string
from it (or from the environment) and writes the generated API role password
and connection string back into the same file, which it keeps at mode 600.
Nothing here prints a value.

The API role's password is generated here (192 random bits, URL-safe, so it
needs no escaping in a connection string) and sent to the server only as a
SCRAM-SHA-256 verifier, so the plaintext never reaches the server's logs or
statement statistics.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import stat
import tempfile
from pathlib import Path
from urllib.parse import quote, urlencode, urlunsplit

from psycopg.conninfo import conninfo_to_dict

DEFAULT_SECRETS_FILE = Path("~/.config/atus/neon.env")
OWNER_URL_KEY = "ATUS_OWNER_DATABASE_URL"
API_PASSWORD_KEY = "ATUS_API_DB_PASSWORD"
API_URL_KEY = "ATUS_API_DATABASE_URL"
PASSWORD_BYTES = 24          # 192 bits (the brief asks for at least 60)
SCRAM_ITERATIONS = 4096      # PostgreSQL's default scram_iterations


class SecretsFileError(RuntimeError):
    pass


def read_env_file(path: Path) -> dict[str, str]:
    """``KEY=value`` pairs of a secrets file (quotes around a value are
    removed). A line that is a bare ``postgres://`` URL counts as
    :data:`OWNER_URL_KEY` (pasting the provider's string as is works)."""
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("postgres://", "postgresql://")):
            values.setdefault(OWNER_URL_KEY, line)
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep or not key.strip().isidentifier():
            raise SecretsFileError(f"{path}: a line is not KEY=value (line content not shown)")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def is_private(path: Path) -> bool:
    """Whether only the owner can read the file (no group/other bits)."""
    return not (path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Set ``updates`` in the secrets file (replacing existing keys, keeping
    every other line), atomically and with mode 600."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lines = path.read_text().splitlines() if path.exists() else []
    pending = dict(updates)
    out = []
    for raw in lines:
        stripped = raw.strip()
        key = stripped.removeprefix("export ").partition("=")[0].strip()
        if key in pending and not stripped.startswith("#"):
            out.append(f"{key}={pending.pop(key)}")
        else:
            out.append(raw)
    out.extend(f"{key}={value}" for key, value in pending.items())
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(out) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    path.chmod(0o600)


def new_password() -> str:
    return secrets.token_urlsafe(PASSWORD_BYTES)


def scram_sha256_verifier(password: str, *, salt: bytes | None = None,
                          iterations: int = SCRAM_ITERATIONS) -> str:
    """The SCRAM-SHA-256 verifier PostgreSQL stores for ``password`` (RFC
    5802/7677; the format of ``pg_authid.rolpassword``). PostgreSQL accepts it
    in ``ALTER ROLE ... PASSWORD`` and stores it unchanged. The password must
    be ASCII (SASLprep is then the identity), as generated passwords are."""
    if not password.isascii():
        raise ValueError("only ASCII passwords are supported")
    salt = salt if salt is not None else secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()

    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode()

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}


def api_database_url(owner_url: str, role: str, password: str) -> str:
    """The API's connection string: the owner's host, port and database with
    the API role's credentials. A remote server gets ``sslmode=verify-full``
    with the system CA store (ADR-011: never ``require`` alone)."""
    params = conninfo_to_dict(owner_url)
    host = params.get("host") or "localhost"
    query = {}
    if host not in LOCAL_HOSTS:
        query["sslmode"] = "verify-full"
        query["sslrootcert"] = "system"
        if params.get("channel_binding"):
            query["channel_binding"] = params["channel_binding"]
    elif params.get("sslmode"):
        query["sslmode"] = params["sslmode"]
    netloc = f"{quote(role, safe='')}:{quote(password, safe='')}@"
    netloc += f"[{host}]" if ":" in host else host
    if params.get("port"):
        netloc += f":{params['port']}"
    path = "/" + quote(params.get("dbname") or "", safe="")
    return urlunsplit(("postgresql", netloc, path, urlencode(query), ""))

