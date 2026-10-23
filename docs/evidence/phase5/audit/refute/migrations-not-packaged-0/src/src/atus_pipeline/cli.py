"""``atus`` command-line interface: one subcommand per pipeline stage.

    atus download            fetch official BLS source files into data/raw/
    atus extract             stage the CSV data members into data/staging/<release>/
    atus validate-source     file-level checks of staged data (pre-load)
    atus migrate             create/upgrade the database schema
    atus load                transform staged data and load it (atomic rebuild)
    atus validate-db         data-quality checks against the loaded database
    atus analyze ...         run analyses with the Phase 2 statistical engine
    atus validate-analytics  reproduce official BLS estimates (benchmark suite)
    atus status              show manifest and database state
"""

from __future__ import annotations

import logging
import sys

import click

from .config import Settings, load_settings
from .database.connection import connect
from .database.migrate import apply_migrations
from .logging_setup import configure_logging
from .manifest import Manifest
from .sources import release_files
from .validation.results import CheckResult, summarize

log = logging.getLogger("atus")


def _settings() -> Settings:
    settings = load_settings()
    configure_logging(settings.log_level)
    return settings


def _print_results(results: list[CheckResult]) -> bool:
    """Print check results; return True when no error-severity check failed."""
    width = max(len(r.name) for r in results)
    for result in results:
        status = "PASS" if result.passed else ("FAIL" if result.severity == "error" else "WARN")
        click.echo(f"  [{status}] {result.name:<{width}}  {result.observed}")
        if not result.passed:
            click.echo(f"         -> {result.detail}")
    passed, errors, warnings = summarize(results)
    click.echo(f"\n{passed} passed, {errors} failed, {warnings} warnings")
    return errors == 0


@click.group(help=__doc__)
def cli() -> None:
    pass


@cli.command(help="Download official BLS source files into data/raw/.")
@click.option("--force", is_flag=True, help="Re-download files that already exist.")
def download(force: bool) -> None:
    from .acquisition.download import download_all

    settings = _settings()
    manifest = Manifest(settings.manifest_path)
    download_all(
        release_files(settings.release),
        release=settings.release,
        raw_dir=settings.raw_dir,
        manifest=manifest,
        force=force,
    )
    click.echo(f"Downloaded release {settings.release} into {settings.raw_dir}")


@cli.command(help="Extract data files from the raw archives into the staging area.")
@click.option("--force", is_flag=True, help="Re-extract files that are already staged.")
def extract(force: bool) -> None:
    from .staging.extract import extract_all

    settings = _settings()
    manifest = Manifest(settings.manifest_path)
    extract_all(
        release_files(settings.release),
        release=settings.release,
        raw_dir=settings.raw_dir,
        staging_dir=settings.staging_dir,
        manifest=manifest,
        force=force,
    )
    click.echo(f"Staged release {settings.release} into {settings.staging_dir}")


@cli.command("validate-source", help="Validate staged source files before loading.")
def validate_source() -> None:
    from .validation.source_checks import validate_all_sources

    settings = _settings()
    results = validate_all_sources(
        release_files(settings.release), settings.staging_dir, settings.release
    )
    if not _print_results(results):
        sys.exit(1)


@cli.command(help="Apply pending database migrations.")
def migrate() -> None:
    settings = _settings()
    with connect(settings.database_url) as conn:
        applied = apply_migrations(conn, migrations_dir=_migrations_dir())
    if applied:
        click.echo("Applied: " + ", ".join(applied))
    else:
        click.echo("Schema already up to date.")


@cli.command(help="Transform staged data and load the canonical schema (atomic rebuild).")
def load() -> None:
    from .loading.loader import load_all

    settings = _settings()
    with connect(settings.database_url) as conn:
        results = load_all(conn, settings)
    click.echo("\nLoaded tables:")
    for result in results:
        click.echo(f"  {result.table:<28} {result.loaded_rows:>12,} rows")


@cli.command("validate-db", help="Run data-quality checks against the loaded database.")
def validate_db() -> None:
    from .validation.db_checks import run_db_checks

    settings = _settings()
    with connect(settings.database_url) as conn:
        results = run_db_checks(conn, settings)
    if not _print_results(results):
        sys.exit(1)


@cli.command(help="Show manifest and database state.")
def status() -> None:
    settings = _settings()
    manifest = Manifest(settings.manifest_path)
    click.echo(f"Release: {settings.release}")
    click.echo(f"Data directory: {settings.data_dir.resolve()}")
    if not manifest.entries:
        click.echo("Manifest: empty (run `atus download`)")
    else:
        click.echo("Manifest:")
        for entry in manifest.entries.values():
            staged = f"staged {entry.data_row_count:,} rows" if entry.staged_at else "not staged"
            click.echo(f"  {entry.key:<20} {entry.zip_name:<26} {staged}")
    try:
        with connect(settings.database_url) as conn:
            row = conn.execute(
                "SELECT id, status, started_at FROM atus.ingestion_runs "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            click.echo(
                f"Database: last ingestion run #{row[0]} "
                f"({row[1]}, started {row[2]:%Y-%m-%d %H:%M})"
                if row else "Database: schema present, no ingestion runs yet"
            )
    except Exception as exc:  # connection/schema problems are status, not crashes
        click.echo(f"Database: unavailable ({type(exc).__name__}: {exc})")


def _migrations_dir():
    from pathlib import Path

    # repo layout: migrations/ next to src/; resolve relative to this file so the
    # CLI works from any working directory inside the repo
    candidate = Path(__file__).resolve().parents[2] / "migrations"
    if not candidate.exists():
        raise click.ClickException(
            f"migrations directory not found at {candidate}; run from the repository root"
        )
    return candidate


@cli.command(help="Run the read-only analytical HTTP API (Phase 3).")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True, type=int)
@click.option("--reload", "reload_", is_flag=True, help="Auto-reload on code changes (dev).")
def api(host: str, port: int, reload_: bool) -> None:
    import uvicorn

    _settings()  # fail fast on malformed configuration
    uvicorn.run(
        "atus_pipeline.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload_,
    )


# Phase 2: analytical engine commands (kept in their own module; imported at
# the bottom because analytics_cli reuses this module's helpers).
from .analytics_cli import analyze, validate_analytics  # noqa: E402

cli.add_command(analyze)
cli.add_command(validate_analytics)


if __name__ == "__main__":
    cli()
