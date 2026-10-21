"""Content-hash bookkeeping, client-tool invocation and artifact verification
(no database: the database side is in tests/integration/test_serving_*.py)."""

from __future__ import annotations

import json
import os

import pytest

from atus_pipeline.serving import pgtools
from atus_pipeline.serving.artifact import (
    ArtifactError,
    artifact_stem,
    manifest_for,
    read_manifest,
    sha256_file,
    verify_artifact,
)
from atus_pipeline.serving.content import (
    HASHED_TABLE_NAMES,
    StreamDigest,
    TableDigest,
    combine,
)


def test_stream_digest_counts_rows_across_chunks():
    digest = StreamDigest()
    for chunk in (b"1\ta\n2\t", memoryview(b"b\n3\tc\\nd\n")):
        digest.update(chunk)
    assert digest.rows == 3          # the escaped newline inside a value is not a row
    whole = StreamDigest()
    whole.update(b"1\ta\n2\tb\n3\tc\\nd\n")
    assert whole.hexdigest() == digest.hexdigest()


def test_combined_hash_covers_names_types_counts_and_order():
    a = TableDigest("t1", (("id", "bigint"),), 2, "aa")
    b = TableDigest("t2", (("w", "double precision"),), 1, "bb")
    assert a.line() == "t1\tid:bigint\t2\taa\n"
    base = combine([a, b]).content_hash
    assert combine([a, b]).content_hash == base
    assert combine([b, a]).content_hash != base
    assert combine([a, TableDigest("t2", (("w", "real"),), 1, "bb")]).content_hash != base
    assert combine([a, TableDigest("t2", (("w", "double precision"),), 2, "bb")]).content_hash \
        != base


def test_every_served_table_is_hashed_and_the_metadata_tables_are_not():
    assert set(HASHED_TABLE_NAMES) == {
        "activity_tier1", "activity_tier2", "activity_codes", "respondents",
        "household_members", "cps_persons", "activities", "replicate_weights",
        "pandemic_replicate_weights",
    }


@pytest.mark.parametrize("text, major", [
    ("pg_dump (PostgreSQL) 16.4", 16),
    ("psql (PostgreSQL) 17.0 (Homebrew)", 17),
    ("pg_restore (PostgreSQL) 16.15 (Debian 16.15-1.pgdg13+2)", 16),
    ("not a version", None),
])
def test_parse_major(text, major):
    assert pgtools.parse_major(text) == major


def test_docker_invocation_names_variables_but_never_their_values():
    env = {"PGHOST": "localhost", "PGPORT": "5466", "PGUSER": "owner",
           "PGPASSWORD": "hunter2", "PGDATABASE": "atus"}
    argv, env_out = pgtools.docker_argv("pg_restore", env, "postgres:16", platform="darwin")
    assert "hunter2" not in " ".join(argv)
    assert argv[-2:] == ("postgres:16", "pg_restore")
    assert ("-e", "PGPASSWORD") == argv[argv.index("PGPASSWORD") - 1:argv.index("PGPASSWORD") + 1]
    assert env_out["PGHOST"] == "host.docker.internal"
    assert env_out["PGPASSWORD"] == "hunter2"
    linux, linux_env = pgtools.docker_argv("psql", env, "postgres:16", platform="linux")
    assert "--network" in linux and linux_env["PGHOST"] == "localhost"
    remote, remote_env = pgtools.docker_argv(
        "psql", {**env, "PGHOST": "ep-1.neon.tech"}, "postgres:16", platform="darwin")
    assert "--network" not in remote and remote_env["PGHOST"] == "ep-1.neon.tech"
    with pytest.raises(pgtools.PgToolError):
        pgtools.docker_argv("psql", {**env, "PGHOST": "/tmp"}, "postgres:16")


def test_resolve_prefers_a_new_enough_local_client(monkeypatch):
    monkeypatch.setenv("PGHOST", "elsewhere")             # never inherited
    monkeypatch.setenv("PGSSLMODE", "disable")
    monkeypatch.setattr(pgtools, "local_major", lambda tool: 16)
    monkeypatch.setattr(pgtools.shutil, "which", lambda name: f"/usr/bin/{name}")
    tool = pgtools.resolve("pg_dump", "postgresql://u:pw@h/db", server_major=16)
    assert tool.argv == ("/usr/bin/pg_dump",)
    assert tool.env["PGPASSWORD"] == "pw" and tool.env["PGHOST"] == "h"
    assert "PGSSLMODE" not in tool.env
    monkeypatch.setattr(pgtools, "local_major", lambda tool: 14)
    tool = pgtools.resolve("pg_dump", "postgresql://u:pw@h/db", server_major=16)
    assert tool.argv[:2] == ("docker", "run") and tool.argv[-2] == "postgres:16"
    monkeypatch.setattr(pgtools.shutil, "which",
                        lambda name: None if name == "docker" else f"/usr/bin/{name}")
    with pytest.raises(pgtools.PgToolError, match="16 or newer"):
        pgtools.resolve("pg_restore", "postgresql://u:pw@h/db", server_major=16)
    with pytest.raises(pgtools.PgToolError, match="pg_dump 16 is needed"):
        pgtools.resolve("pg_dump", "postgresql://u:pw@h/db", server_major=16)


def test_pg_dump_is_exactly_the_servers_major_version(monkeypatch):
    # A pg_dump newer than the server writes an archive the deploy runner's
    # pg_restore 16 cannot read (and SET commands a 16 server rejects).
    monkeypatch.setattr(pgtools, "local_major", lambda tool: 17)
    monkeypatch.setattr(pgtools.shutil, "which", lambda name: f"/usr/bin/{name}")
    dump = pgtools.resolve("pg_dump", "postgresql://u:pw@h/db", server_major=16)
    assert dump.argv[:2] == ("docker", "run") and dump.argv[-2:] == ("postgres:16", "pg_dump")
    restore = pgtools.resolve("pg_restore", "postgresql://u:pw@h/db", server_major=16)
    assert restore.argv == ("/usr/bin/pg_restore",)
    monkeypatch.setattr(pgtools, "local_major", lambda tool: 16)
    assert pgtools.resolve("pg_dump", "postgresql://u:pw@h/db",
                           server_major=16).argv == ("/usr/bin/pg_dump",)


@pytest.mark.parametrize("recorded, server, expected", [
    ("pg_dump (PostgreSQL) 16.15 (Debian 16.15-1.pgdg13+2) (docker postgres:16)", 16, 16),
    ("pg_dump (PostgreSQL) 18.1 (local /opt/homebrew/bin/pg_dump (18))", 16, 18),
    (None, 16, 16),
    ("pg_dump (PostgreSQL) 16.4", 17, 17),
])
def test_the_restore_client_is_at_least_as_new_as_the_dump(recorded, server, expected):
    from atus_pipeline.serving.restore import restore_client_major

    manifest = {"tools": {"pg_dump": recorded}} if recorded else {}
    assert restore_client_major(server, manifest) == expected


NEON_ENV = {"PGHOST": "ep-1.us-east-1.aws.neon.tech", "PGUSER": "neondb_owner",
            "PGPASSWORD": "hunter2", "PGDATABASE": "atus", "PGSSLMODE": "verify-full"}


def test_the_container_gets_the_hosts_ca_bundle_for_sslrootcert_system(tmp_path):
    # The postgres image ships no CA bundle (ca-certificates is not
    # installed), so `system` inside the container needs the host's.
    bundle = tmp_path / "cert.pem"
    bundle.write_text("roots")
    env = {**NEON_ENV, "PGSSLROOTCERT": "system"}
    argv, env_out = pgtools.docker_argv("pg_restore", env, "postgres:16", platform="darwin",
                                        ca_file=str(bundle))
    mount = f"{bundle.resolve()}:{pgtools.CONTAINER_CA_FILE}:ro"
    assert argv[argv.index(mount) - 1] == "-v"
    assert argv[argv.index(f"SSL_CERT_FILE={pgtools.CONTAINER_CA_FILE}") - 1] == "-e"
    assert env_out["PGSSLROOTCERT"] == "system"        # verification is unchanged
    assert argv[-2:] == ("postgres:16", "pg_restore")
    with pytest.raises(pgtools.PgToolError, match="SSL_CERT_FILE"):
        pgtools.docker_argv("psql", env, "postgres:16", platform="darwin", ca_file=None)
    plain, _ = pgtools.docker_argv("psql", NEON_ENV, "postgres:16", platform="darwin",
                                   ca_file=str(bundle))
    assert "-v" not in plain


def test_the_container_gets_a_sslrootcert_file_mounted(tmp_path):
    ca = tmp_path / "neon-ca.pem"
    ca.write_text("ca")
    argv, env_out = pgtools.docker_argv(
        "psql", {**NEON_ENV, "PGSSLROOTCERT": str(ca)}, "postgres:16", platform="darwin")
    assert f"{ca.resolve()}:{pgtools.CONTAINER_ROOT_CERT}:ro" in argv
    assert env_out["PGSSLROOTCERT"] == pgtools.CONTAINER_ROOT_CERT
    with pytest.raises(pgtools.PgToolError, match="not found"):
        pgtools.docker_argv("psql", {**NEON_ENV, "PGSSLROOTCERT": str(tmp_path / "x.pem")},
                            "postgres:16", platform="darwin")


def test_resolve_hands_the_container_the_bundle_ssl_cert_file_names(tmp_path, monkeypatch):
    bundle = tmp_path / "cert.pem"
    bundle.write_text("roots")
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    monkeypatch.setattr(pgtools, "local_major", lambda tool: None)
    monkeypatch.setattr(pgtools.shutil, "which", lambda name: f"/usr/bin/{name}")
    tool = pgtools.resolve(
        "pg_restore", "postgresql://o:pw@ep-1.neon.tech/atus?sslmode=verify-full"
                      "&sslrootcert=system", server_major=16)
    assert f"{bundle.resolve()}:{pgtools.CONTAINER_CA_FILE}:ro" in tool.argv


def _artifact(tmp_path, content=b"PGDMP fake dump"):
    stem = artifact_stem("0325", "20261020T000000Z-0123abcd")
    dump = tmp_path / f"{stem}.dump"
    dump.write_bytes(content)
    digest = sha256_file(dump)
    (tmp_path / f"{stem}.dump.sha256").write_text(f"{digest}  {dump.name}\n")
    manifest = {
        "kind": "atus-serving-db", "data_version": "0325:serving-x:abc", "content_hash": "abc",
        "release": "0325", "build_id": "20261020T000000Z-0123abcd", "tables": {},
        "artifact": {"file": dump.name, "bytes": len(content), "sha256": digest},
    }
    manifest_path = manifest_for(dump)
    manifest_path.write_text(json.dumps(manifest))
    return dump, manifest_path


def test_verify_artifact_accepts_an_intact_dump(tmp_path):
    dump, manifest_path = _artifact(tmp_path)
    assert manifest_path.name == "atus-serving-0325-20261020T000000Z-0123abcd.manifest.json"
    manifest = read_manifest(manifest_path)
    assert verify_artifact(dump, manifest) == manifest["artifact"]["sha256"]


def test_verify_artifact_refuses_a_changed_dump(tmp_path):
    dump, manifest_path = _artifact(tmp_path)
    manifest = read_manifest(manifest_path)
    dump.write_bytes(b"PGDMP fake dumq")
    with pytest.raises(ArtifactError, match="SHA-256 does not match the manifest"):
        verify_artifact(dump, manifest)


def test_verify_artifact_refuses_a_disagreeing_checksum_file(tmp_path):
    dump, manifest_path = _artifact(tmp_path)
    (tmp_path / f"{dump.name}.sha256").write_text(f"{'0' * 64}  {dump.name}\n")
    with pytest.raises(ArtifactError, match=r"\.sha256"):
        verify_artifact(dump, read_manifest(manifest_path))


def test_verify_artifact_refuses_another_build(tmp_path):
    dump, manifest_path = _artifact(tmp_path)
    manifest = read_manifest(manifest_path)
    manifest["artifact"]["file"] = "atus-serving-0325-other.dump"
    with pytest.raises(ArtifactError, match="describes"):
        verify_artifact(dump, manifest)


def test_read_manifest_refuses_other_json(tmp_path):
    path = tmp_path / "x.manifest.json"
    path.write_text(json.dumps({"kind": "something-else"}))
    with pytest.raises(ArtifactError):
        read_manifest(path)


@pytest.mark.parametrize(("content", "message"), [
    (None, "missing or unreadable .*keep the release's .manifest.json beside"),
    ("{not json", "missing or unreadable .*keep the release's .manifest.json beside"),
    ("\udcff", "missing or unreadable .*keep the release's .manifest.json beside"),
    ("[1, 2]", "is not an atus-serving-db manifest"),
], ids=["missing", "corrupt", "undecodable", "not an object"])
def test_read_manifest_refuses_a_missing_or_unreadable_file_cleanly(tmp_path, content, message):
    """Review r2 (restore-missing-manifest-traceback): a missing or corrupt
    manifest raised FileNotFoundError/JSONDecodeError (a JSON list,
    AttributeError), which no command catches, so restore-serving-db ended
    in a traceback."""
    dump, manifest_path = _artifact(tmp_path)
    manifest_path.unlink()
    if content is not None:
        manifest_path.write_bytes(content.encode("utf-8", "surrogateescape"))
    with pytest.raises(ArtifactError, match=message):
        read_manifest(manifest_for(dump))


@pytest.mark.parametrize("owner_query", ["sslmode=verify-full&sslrootcert=system",
                                         "sslmode=require&channel_binding=require"])
def test_the_restore_finds_ca_roots_before_its_first_connection(tmp_path, monkeypatch,
                                                                owner_query):
    # Owner step 3.5 on the owner's Mac: the first connection (the owner's
    # DSN) and the API role checks (always verify-full + system for a remote
    # host) need roots that the psycopg wheel's OpenSSL cannot find alone.
    from atus_pipeline.database import dsn
    from atus_pipeline.serving import restore as restore_module

    bundle = tmp_path / "cert.pem"
    bundle.write_text("roots")
    monkeypatch.setattr(dsn, "SYSTEM_CA_BUNDLES", (str(bundle),), raising=False)
    # Unset now and after the test: the code under test sets it, and a
    # delenv of an unset variable records nothing to undo.
    monkeypatch.setenv("SSL_CERT_FILE", "unset-below")
    monkeypatch.delenv("SSL_CERT_FILE")
    monkeypatch.setenv("ATUS_OWNER_DATABASE_URL",
                       f"postgresql://neondb_owner:pw@ep-1.us-east-1.aws.neon.tech/atus"
                       f"?{owner_query}")
    seen = []

    def connect(*args, **kwargs):
        seen.append(os.environ.get("SSL_CERT_FILE"))
        raise RuntimeError("stop before any network")

    monkeypatch.setattr(restore_module.psycopg, "connect", connect)
    dump, _ = _artifact(tmp_path)
    options = restore_module.RestoreOptions(artifact=dump, secrets_file=tmp_path / "neon.env")
    with pytest.raises(RuntimeError, match="stop before any network"):
        restore_module.restore(options, echo=lambda message: None)
    assert seen == [str(bundle)]
