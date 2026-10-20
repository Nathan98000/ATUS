"""Content-hash bookkeeping, client-tool invocation and artifact verification
(no database: the database side is in tests/integration/test_serving_*.py)."""

from __future__ import annotations

import json

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
        pgtools.resolve("pg_dump", "postgresql://u:pw@h/db", server_major=16)


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
