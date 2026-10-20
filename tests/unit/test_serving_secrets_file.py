"""The owner's secrets file, the API role's password and its SCRAM verifier."""

from __future__ import annotations

import base64
import hashlib
import hmac
import stat

import pytest

from atus_pipeline.serving.secrets_file import (
    API_URL_KEY,
    OWNER_URL_KEY,
    PASSWORD_BYTES,
    SecretsFileError,
    api_database_url,
    is_private,
    new_password,
    read_env_file,
    scram_sha256_verifier,
    update_env_file,
)


def test_read_env_file_accepts_key_value_lines_and_a_pasted_url(tmp_path):
    path = tmp_path / "neon.env"
    path.write_text(
        "# owner secrets\n"
        "\n"
        "export ATUS_X='quoted value'\n"
        'ATUS_Y="double"\n'
        "ATUS_Z=a=b\n"
        "postgresql://neondb_owner:pw@ep-1.neon.tech/atus?sslmode=require\n"
    )
    values = read_env_file(path)
    assert values == {
        "ATUS_X": "quoted value", "ATUS_Y": "double", "ATUS_Z": "a=b",
        OWNER_URL_KEY: "postgresql://neondb_owner:pw@ep-1.neon.tech/atus?sslmode=require",
    }


def test_a_malformed_line_is_refused_without_echoing_it(tmp_path):
    path = tmp_path / "neon.env"
    path.write_text("not a key value line with hunter2\n")
    with pytest.raises(SecretsFileError) as excinfo:
        read_env_file(path)
    assert "hunter2" not in str(excinfo.value)


def test_update_env_file_replaces_keys_keeps_the_rest_and_is_private(tmp_path):
    path = tmp_path / "cfg" / "neon.env"
    path.parent.mkdir()
    path.write_text(f"# keep me\n{OWNER_URL_KEY}=postgresql://o:p@h/db\nATUS_API_DB_PASSWORD=old\n")
    path.chmod(0o644)
    update_env_file(path, {"ATUS_API_DB_PASSWORD": "new", API_URL_KEY: "postgresql://a:b@h/db"})
    assert path.read_text().splitlines() == [
        "# keep me",
        f"{OWNER_URL_KEY}=postgresql://o:p@h/db",
        "ATUS_API_DB_PASSWORD=new",
        f"{API_URL_KEY}=postgresql://a:b@h/db",
    ]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert is_private(path)
    assert [p.name for p in path.parent.iterdir()] == ["neon.env"]   # no temp file left


def test_update_env_file_creates_a_missing_file_privately(tmp_path):
    path = tmp_path / "new" / "neon.env"
    update_env_file(path, {"K": "v"})
    assert path.read_text() == "K=v\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_generated_passwords_have_at_least_60_bits_and_need_no_escaping():
    passwords = {new_password() for _ in range(50)}
    assert len(passwords) == 50
    assert PASSWORD_BYTES * 8 >= 60
    for password in passwords:
        assert len(password) >= 32                         # 24 random bytes, base64url
        assert set(password) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                                    "0123456789-_")


def test_scram_verifier_matches_the_rfc_7677_example():
    """RFC 7677 section 3: user "user", password "pencil". The server signature
    computed from our ServerKey over the example's AuthMessage must equal the
    RFC's, and the client proof from our StoredKey must verify."""
    salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
    verifier = scram_sha256_verifier("pencil", salt=salt, iterations=4096)
    mechanism, rest = verifier.split("$", 1)
    params, keys = rest.split("$")
    assert mechanism == "SCRAM-SHA-256"
    assert params == "4096:W22ZaJ0SNY7soEsUEjb6gQ=="
    stored_key, server_key = (base64.b64decode(k) for k in keys.split(":"))

    nonce = "rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0"
    auth_message = (
        "n=user,r=rOprNGfwEbeRWgbNEkqO,"
        f"r={nonce},s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,"
        f"c=biws,r={nonce}"
    ).encode()
    server_signature = hmac.new(server_key, auth_message, "sha256").digest()
    assert base64.b64encode(server_signature).decode() == (
        "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="
    )
    proof = base64.b64decode("dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ=")
    client_signature = hmac.new(stored_key, auth_message, "sha256").digest()
    client_key = bytes(a ^ b for a, b in zip(proof, client_signature, strict=True))
    assert hashlib.sha256(client_key).digest() == stored_key


def test_scram_verifier_uses_a_fresh_salt_and_refuses_non_ascii():
    assert scram_sha256_verifier("same password") != scram_sha256_verifier("same password")
    with pytest.raises(ValueError):
        scram_sha256_verifier("pässword")


def test_api_url_for_a_remote_server_verifies_the_certificate():
    url = api_database_url(
        "postgresql://neondb_owner:pw@ep-cool-1.us-east-1.aws.neon.tech/atus"
        "?sslmode=require&channel_binding=require",
        "atus_api", "Xy-_9",
    )
    assert url == (
        "postgresql://atus_api:Xy-_9@ep-cool-1.us-east-1.aws.neon.tech/atus"
        "?sslmode=verify-full&sslrootcert=system&channel_binding=require"
    )


def test_api_url_for_a_local_server_keeps_the_owner_ssl_choice():
    assert api_database_url("postgresql://o:p@localhost:5466/atus", "atus_api", "pw") == (
        "postgresql://atus_api:pw@localhost:5466/atus"
    )
    assert api_database_url("postgresql://o:p@[::1]:5466/atus?sslmode=disable", "r", "pw") == (
        "postgresql://r:pw@[::1]:5466/atus?sslmode=disable"
    )
