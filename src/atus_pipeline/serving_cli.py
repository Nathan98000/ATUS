"""``atus`` commands for the serving database (docs/adr/ADR-013).

    atus build-serving-db    build the serving DB from the full DB (+ release artifact)
    atus verify-serving-db   check a built or restored serving DB
    atus restore-serving-db  restore a release artifact (scripts/ops/restore_serving_db.sh)
    atus compare-serving-db  randomized full-vs-serving equivalence diff

Connection strings come from options or the environment and are never
printed: messages name databases as ``user@host:port/dbname``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click
import psycopg
from psycopg.conninfo import conninfo_to_dict

from .cli import _print_results, _settings
from .database.dsn import describe, redact

SERVING_URL_ENV = "ATUS_SERVING_DATABASE_URL"


def _fail(exc: Exception, *secrets: str) -> click.ClickException:
    first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return click.ClickException(redact(first_line, *secrets))


@click.command(
    "build-serving-db",
    help=(
        "Build the serving database from the validated full database (read only) into a "
        "new, empty database, and optionally dump it as a checksummed release artifact."
    ),
)
@click.option("--source", "source_url", default=None,
              help="Full database (read only). Default: $ATUS_DATABASE_URL.")
@click.option("--target", "target_url", default=None,
              help=f"New, empty database to build into. Default: ${SERVING_URL_ENV}.")
@click.option("--artifact-dir", type=click.Path(file_okay=False, path_type=Path),
              default=None,
              help="Also write the release artifact (pg_dump -Fc, .sha256, manifest) here. "
                   "Keep it outside the repository.")
@click.option("--allow-unvalidated", is_flag=True,
              help="Build even without passing validate-db/validate-analytics results "
                   "recorded for the source's data (test fixtures only).")
@click.option("--build-id", default=None, hidden=True,
              help="Override the generated build id.")
@click.option("--pg-client-image", default=None,
              help="Docker image for pg_dump when no local client is new enough "
                   "(default postgres:<server major>).")
@click.option("--report", "report_path", type=click.Path(dir_okay=False, path_type=Path),
              default=None, help="Write the build report (JSON) here.")
def build_serving_db(source_url, target_url, artifact_dir, allow_unvalidated, build_id,
                     pg_client_image, report_path) -> None:
    from .serving.artifact import ArtifactError, write_artifact
    from .serving.builder import ServingBuildError, build_serving_database
    from .serving.pgtools import PgToolError

    settings = _settings()
    source_url = source_url or settings.database_url
    target_url = target_url or os.environ.get(SERVING_URL_ENV)
    if not target_url:
        raise click.ClickException(f"pass --target or set {SERVING_URL_ENV}")
    click.echo(f"Source (read only): {describe(source_url)}")
    click.echo(f"Target:             {describe(target_url)}")
    try:
        report = build_serving_database(
            source_url, target_url, build_id=build_id, require_validated=not allow_unvalidated
        )
    except (ServingBuildError, psycopg.Error) as exc:
        raise _fail(exc, source_url, target_url) from exc

    click.echo(f"\nData version:  {report.data_version}")
    click.echo(f"Content hash:  {report.content_hash}")
    click.echo(f"Source:        {report.source['data_version']}")
    click.echo("Served tables:")
    for table, info in report.tables.items():
        if "rows" in info:
            click.echo(f"  {table:<28} {info['rows']:>12,} rows  {info['total_bytes']:>14,} B")
    omitted = {k: v for k, v in report.counts.items() if k.endswith(".omitted")}
    click.echo("Omitted rows (excluded by every estimate): "
               + ", ".join(f"{k.removesuffix('.omitted')} {v:,}" for k, v in omitted.items()))
    click.echo(f"Database size: {report.sizes['database_bytes']:,} B")
    if artifact_dir:
        try:
            manifest = write_artifact(target_url, artifact_dir, report, image=pg_client_image)
        except (ArtifactError, PgToolError, psycopg.Error) as exc:
            raise _fail(exc, source_url, target_url) from exc
        info = json.loads(manifest.read_text())["artifact"]
        click.echo(f"Artifact:      {artifact_dir / info['file']} ({info['bytes']:,} B)")
        click.echo(f"SHA-256:       {info['sha256']}")
        click.echo(f"Manifest:      {manifest}")
    if report_path:
        report_path.write_text(json.dumps(report.as_dict(), indent=2, default=str) + "\n")


@click.command("verify-serving-db",
               help="Check a built or restored serving database (read only).")
@click.option("--database-url", default=None,
              help="Serving database. Default: $ATUS_DATABASE_URL.")
@click.option("--manifest", "manifest_path", type=click.Path(dir_okay=False, path_type=Path),
              default=None, help="Release manifest to check the data version and rows against.")
@click.option("--content-hash", is_flag=True,
              help="Re-hash every served row (reads the whole database: egress on a "
                   "metered host).")
@click.option("--explain", is_flag=True, help="Check the worst-case query plans.")
def verify_serving_db(database_url, manifest_path, content_hash, explain) -> None:
    from .serving.artifact import read_manifest
    from .serving.checks import serving_checks

    settings = _settings()
    url = database_url or settings.database_url
    manifest = read_manifest(manifest_path) if manifest_path else None
    click.echo(f"Serving database: {describe(url)}")
    try:
        with psycopg.connect(url, autocommit=True,
                             options="-c default_transaction_read_only=on") as conn:
            results = serving_checks(conn, manifest, content_hash=content_hash, explain=explain)
    except psycopg.Error as exc:
        raise _fail(exc, url) from exc
    if not _print_results(results):
        sys.exit(1)


@click.command(
    "restore-serving-db",
    help=(
        "Restore a serving-database release artifact into a new, empty database, apply "
        "scripts/ops/roles.sql, VACUUM (ANALYZE), and check the result. The owner's "
        "connection string comes from $ATUS_OWNER_DATABASE_URL or the secrets file; the "
        "API role's password and connection string are written back into that file."
    ),
)
@click.option("--artifact", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              required=True, help="The .dump file (its .sha256 and manifest beside it).")
@click.option("--secrets-file", type=click.Path(dir_okay=False, path_type=Path),
              default=Path("~/.config/atus/neon.env"), show_default=True,
              help="Owner's KEY=value secrets file (outside the repository, mode 600).")
@click.option("--api-role", default="atus_api", show_default=True)
@click.option("--rotate-api-password", is_flag=True,
              help="Generate a new API role password even if the secrets file has one.")
@click.option("--roles-only", is_flag=True,
              help="Skip the restore: re-apply roles.sql (with --rotate-api-password, rotate) "
                   "and re-run the checks on an already restored serving database (one "
                   "without the serving marker is refused).")
@click.option("--size-limit-bytes", type=int, default=1_000_000_000, show_default=True,
              help="Fail if sum(pg_database_size) exceeds this (0 disables the check).")
@click.option("--verify-content-hash", is_flag=True,
              help="Re-hash every served row against the manifest (reads the whole "
                   "database: egress on a metered host).")
@click.option("--roles-sql", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None, help="Path of scripts/ops/roles.sql.")
@click.option("--pg-client-image", default=None,
              help="Docker image for pg_restore/psql when no local client is new enough.")
@click.option("--report", "report_path", type=click.Path(dir_okay=False, path_type=Path),
              default=None, help="Write the restore report (JSON, no secrets) here.")
def restore_serving_db(artifact, secrets_file, api_role, rotate_api_password, roles_only,
                       size_limit_bytes, verify_content_hash, roles_sql, pg_client_image,
                       report_path) -> None:
    from .serving.artifact import ArtifactError
    from .serving.pgtools import PgToolError
    from .serving.restore import (
        DEFAULT_ROLES_SQL,
        RestoreError,
        RestoreOptions,
        owner_url,
        restore,
        write_report,
    )
    from .serving.secrets_file import SecretsFileError

    _settings()
    options = RestoreOptions(
        artifact=artifact, secrets_file=secrets_file, api_role=api_role,
        rotate_api_password=rotate_api_password, size_limit_bytes=size_limit_bytes,
        verify_content_hash=verify_content_hash, roles_sql=roles_sql or DEFAULT_ROLES_SQL,
        pg_client_image=pg_client_image, roles_only=roles_only,
    )
    try:
        url = owner_url(options)
    except (RestoreError, SecretsFileError) as exc:
        raise _fail(exc) from exc
    try:
        report = restore(options, echo=click.echo)
    except (RestoreError, ArtifactError, PgToolError, SecretsFileError, psycopg.Error) as exc:
        raise _fail(exc, url, conninfo_to_dict(url).get("password") or "") from exc
    if report_path:
        write_report(report, report_path, url)
    click.echo("Restore " + ("passed." if report.passed else "FAILED."))
    if not report.passed:
        sys.exit(1)


@click.command(
    "compare-serving-db",
    help=(
        "Randomized full-vs-serving diff (brief §9): run the same specifications on both "
        "databases (read only) and compare them under the equivalence criterion."
    ),
)
@click.option("--full", "full_url", default=None,
              help="Full database (read only). Default: $ATUS_DATABASE_URL.")
@click.option("--serving", "serving_url", default=None,
              help=f"Serving database (read only). Default: ${SERVING_URL_ENV}.")
@click.option("--specs", "n_specs", type=int, default=200, show_default=True,
              help="Random specifications, besides the fixed extreme cases.")
@click.option("--seed", type=int, default=20261020, show_default=True)
@click.option("--report", "report_path", type=click.Path(dir_okay=False, path_type=Path),
              default=None, help="Write every comparison (JSON) here.")
def compare_serving_db(full_url, serving_url, n_specs, seed, report_path) -> None:
    import random

    from .analytics import AnalysisEngine
    from .serving.equivalence import (
        Comparison,
        compare_results,
        random_specs,
        run,
        seed_specs,
        spec_dict,
    )

    settings = _settings()
    full_url = full_url or settings.database_url
    serving_url = serving_url or os.environ.get(SERVING_URL_ENV)
    if not serving_url:
        raise click.ClickException(f"pass --serving or set {SERVING_URL_ENV}")
    click.echo(f"Full:    {describe(full_url)}")
    click.echo(f"Serving: {describe(serving_url)}")
    options = "-c default_transaction_read_only=on -c jit=off"
    total = Comparison()
    rows = []
    try:
        with psycopg.connect(full_url, autocommit=True, options=options) as full, \
                psycopg.connect(serving_url, autocommit=True, options=options) as serving:
            years = [y for (y,) in full.execute(
                "SELECT DISTINCT data_year FROM atus.respondents ORDER BY 1")]
            tier1 = [c for (c,) in full.execute(
                "SELECT code FROM atus.activity_tier1 ORDER BY 1")]
            leaves = [c for (c,) in full.execute(
                "SELECT code FROM atus.activity_codes ORDER BY 1")]
            specs = seed_specs(years, tier1) + random_specs(
                random.Random(seed), years=years, tier1=tier1, leaves=leaves, n=n_specs)
            engines = AnalysisEngine(full), AnalysisEngine(serving)
            for i, (operation, spec) in enumerate(specs, 1):
                a, b = (run(engine, operation, spec) for engine in engines)
                result = compare_results(a, b)
                total.merge(result)
                rows.append({"operation": operation, "spec": spec_dict(spec),
                             "equivalent": result.equivalent,
                             "max_relative": result.max_relative,
                             "mismatches": result.mismatches,
                             "error": a if isinstance(a, str) else None})
                if i % 25 == 0:
                    click.echo(f"  {i}/{len(specs)} compared")
    except psycopg.Error as exc:
        raise _fail(exc, full_url, serving_url) from exc
    failed = [r for r in rows if not r["equivalent"]]
    click.echo(f"\n{len(rows)} specifications ({sum(r['error'] is not None for r in rows)} "
               f"refusals), {total.compared:,} values compared: "
               f"{len(rows) - len(failed)} equivalent, {len(failed)} not")
    click.echo(f"Largest relative difference on SE/CI: {total.max_relative:.3g}; "
               f"scaled by the estimate: {total.max_scaled:.3g} (bound 1e-12)")
    for row in failed[:20]:
        click.echo(f"  NOT EQUIVALENT {row['operation']}: {row['mismatches'][:3]}")
    if report_path:
        report_path.write_text(json.dumps({"seed": seed, "specs": len(rows),
                                           "max_relative": total.max_relative,
                                           "max_scaled": total.max_scaled,
                                           "results": rows}, indent=1, default=str) + "\n")
    if failed:
        sys.exit(1)


COMMANDS = (build_serving_db, verify_serving_db, restore_serving_db, compare_serving_db)
