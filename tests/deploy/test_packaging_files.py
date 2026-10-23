"""Static checks on the packaging files: Dockerfiles, ignore files, config templates.

F09 (image), F31 (.gitignore), F44/I13 (configuration), supply-chain pinning
(brief §11). Read-only; needs neither Docker nor the network.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from .conftest import REPO_ROOT, worker_env_names, wrangler_vars

DOCKERFILES = ["Dockerfile", "deploy/pgbouncer/Dockerfile", "deploy/edge/Dockerfile"]
DIGEST_ARG = re.compile(r"^ARG \w+_DIGEST=sha256:[0-9a-f]{64}$", re.MULTILINE)

# Every API variable in the implementation contract (CONTRACT.md "Configuration").
CONTRACT_API_VARIABLES = [
    "ATUS_ENV", "ATUS_DATABASE_URL", "ATUS_LOG_LEVEL", "ATUS_LOG_FORMAT", "ATUS_API_DOCS",
    "ATUS_EDGE_SECRETS", "ATUS_EXPECTED_DATA_VERSION", "ATUS_OPS_TOKEN",
    "ATUS_API_CORS_ORIGINS", "ATUS_API_CACHE_SIZE", "ATUS_API_POOL_MAX_SIZE",
    "ATUS_API_POOL_MAX_IDLE_S", "ATUS_API_POOL_TIMEOUT_S", "ATUS_API_CONNECT_TIMEOUT_S",
    "ATUS_API_STATEMENT_TIMEOUT_MS", "ATUS_API_META_STATEMENT_TIMEOUT_MS", "ATUS_API_JIT",
    "ATUS_API_MAX_PARALLEL_WORKERS", "ATUS_API_MAX_INFLIGHT", "ATUS_API_MAX_INFLIGHT_HEAVY",
    "ATUS_API_ADMISSION_WAIT_S", "ATUS_API_RATE_ANALYSIS", "ATUS_API_RATE_REQUESTS",
    "ATUS_API_DAILY_ANALYSIS_CAP", "ATUS_API_MAX_BODY_BYTES", "ATUS_API_THREADS",
    "ATUS_API_SNAPSHOT_TTL_S", "ATUS_BUILD_SHA", "PORT",
]
# ADR-011 / docs/deployment.md names for the other components.
WORKER_SECRETS = ["EDGE_AUTH_SECRET", "OPS_TOKEN", "IP_HMAC_KEY", "RENDER_ORIGIN", "NEON_API_KEY",
                  "API_PAUSED_UNTIL", "API_PAUSED_REASON"]
WORKER_VARS = ["DATA_VERSION", "ANALYTICS_VERSION", "RESPONSE_FORMAT", "SNAPSHOT_ID", "PREWAKE"]
GITHUB_SECRETS = [
    "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "RENDER_DEPLOY_HOOK_URL", "RENDER_ORIGIN",
    "ATUS_OPS_TOKEN", "EDGE_OPS_TOKEN", "PUBLIC_BASE_URL", "GHCR_IMAGE",
]


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text()


# --- Dockerfiles -------------------------------------------------------------------


@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_every_base_image_is_pinned_by_digest(dockerfile):
    text = _read(dockerfile)
    froms = re.findall(r"^FROM (\S+)", text, re.MULTILINE)
    assert froms
    for ref in froms:
        assert ref.endswith("_DIGEST}") or re.search(r"@sha256:[0-9a-f]{64}$", ref), ref
    if any(ref.endswith("_DIGEST}") for ref in froms):
        assert DIGEST_ARG.search(text)
    # A `# syntax=` line would pull a BuildKit frontend by tag at build time.
    assert not text.startswith("# syntax=")


def test_api_image_installs_only_hash_locked_wheels():
    text = _read("Dockerfile").replace("\\\n", " ")  # join continuation lines
    installs = re.findall(r"pip install[^\n&]*", text)
    lock_installs = [i for i in installs if "-r requirements/" in i]
    assert len(lock_installs) == 2
    for command in lock_installs:
        assert "--require-hashes" in command and "--only-binary=:all:" in command
    # The project wheel itself is local: built from the copied sources, no index.
    assert "pip install --no-deps --no-index /dist/atus_pipeline-" in text


def test_api_image_runtime_contract():
    text = _read("Dockerfile")
    runtime = text.split("AS runtime", 1)[1]
    assert re.search(r"^USER 10001:10001$", runtime, re.MULTILINE)
    assert "COPY --from=build /opt/venv /opt/venv" in runtime
    # nothing from the source tree is copied into the runtime stage
    assert re.findall(r"^COPY (?!--from=build)", runtime, re.MULTILINE) == []
    assert "HEALTHCHECK" in runtime and "/health" in runtime
    assert 'CMD ["sh", "-c", "exec atus api --host 0.0.0.0 --port \\"${PORT}\\""]' in runtime
    for env in ("ATUS_ENV=production", "PORT=10000", "WEB_CONCURRENCY=1",
                "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
                "ATUS_BUILD_SHA=${ATUS_BUILD_SHA}"):
        assert env in runtime, env
    for label in ("source", "revision", "version", "licenses", "base.name", "base.digest"):
        assert f"org.opencontainers.image.{label}=" in runtime, label


def test_api_image_compiles_the_standard_library_before_dropping_root():
    """The base image ships the stdlib without .pyc and the app user can never
    write one: the runtime stage compiles it as root, at the base image's
    Python version (compileall silently skips a directory that does not exist,
    so a base bump must move this path too; check_image.py `bytecode` would
    then fail)."""
    text = _read("Dockerfile")
    runtime = text.split("AS runtime", 1)[1]
    compile_step = re.search(
        r"^RUN python -m compileall -q -j 0 -x '/site-packages/' "
        r"/usr/local/lib/python(\d+\.\d+)$", runtime, re.MULTILINE)
    assert compile_step, "the runtime stage must compile the standard library"
    base = re.search(r"^ARG PYTHON_BASE=python:(\d+\.\d+)-slim$", text, re.MULTILINE)
    assert compile_step.group(1) == base.group(1)
    assert runtime.index(compile_step.group(0)) < runtime.index("\nUSER 10001:10001")
    assert "PYTHONDONTWRITEBYTECODE=1" in runtime  # reading .pyc still works


def test_dockerignore_is_an_allow_list_of_what_the_image_needs():
    lines = [
        line.strip() for line in _read(".dockerignore").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines[0] == "*"
    allowed = {line[1:] for line in lines if line.startswith("!")}
    assert allowed == {
        "pyproject.toml", "README.md", "LICENSE", "src/",
        "requirements/api.lock.txt", "requirements/build.lock.txt",
    }
    copied = set()
    for sources in re.findall(r"^COPY (?!--from)([^\n]+)", _read("Dockerfile"), re.MULTILINE):
        copied.update(sources.split()[:-1])
    for source in copied:
        assert any(source == a.rstrip("/") or source.startswith(a) for a in allowed), source


def test_edge_build_context_sends_only_the_worker():
    lines = [
        line.strip() for line in _read("deploy/edge/Dockerfile.dockerignore").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines[0] == "*"
    assert {line for line in lines if line.startswith("!")} == {
        "!edge/", "!deploy/edge/entrypoint.sh", "!deploy/edge/origin-relay.mjs"
    }
    assert "edge/node_modules" in lines and "edge/.dev.vars*" in lines


def test_no_migrations_or_tests_reach_the_image():
    """F56: the API never migrates; the image ships no migrations, tests or data."""
    text = _read("Dockerfile")
    for path in ("migrations", "tests", "data/", "frontend", "scripts"):
        assert f"COPY {path}" not in text


# --- .gitignore (F31) -------------------------------------------------------------


@pytest.fixture(scope="module")
def check_ignore():
    if shutil.which("git") is None:
        pytest.skip("git is not installed")

    def ignored(path: str) -> bool:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", path],
            cwd=REPO_ROOT, capture_output=True, check=False,
        )
        assert result.returncode in (0, 1), result.stderr
        return result.returncode == 0

    return ignored


@pytest.mark.parametrize(
    "path",
    [
        ".env", ".env.local", ".env.production", ".env.production.local", ".env.prod",
        ".env.stack", "secrets.env", "edge.env", "deploy/stack/.env.stack",
        "frontend/.env", "frontend/.env.local", "frontend/.env.production",
        "edge/.dev.vars", "edge/.dev.vars.production", "edge/.wrangler/state/v3/kv.sqlite",
    ],
)
def test_secret_files_are_ignored(check_ignore, path):
    assert check_ignore(path)


@pytest.mark.parametrize(
    "path",
    [
        ".env.example", ".env.production.example", "frontend/.env.example",
        "deploy/stack/stack.env.example", "requirements/api.lock.txt",
    ],
)
def test_templates_are_not_ignored(check_ignore, path):
    assert not check_ignore(path)


# --- configuration templates (F44, §8, §19) ---------------------------------------


def _assignments(text: str) -> dict[str, str]:
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", text, re.MULTILINE))


def test_production_example_documents_every_contract_variable():
    text = _read(".env.production.example")
    for name in CONTRACT_API_VARIABLES:
        assert re.search(rf"^#? ?{name}=", text, re.MULTILINE), name


def test_production_example_values_match_the_contract():
    """Active lines carry the contract's production values (CONTRACT.md "Configuration");
    settings without a fixed production value are commented out."""
    values = _assignments(_read(".env.production.example"))
    assert values == {
        "ATUS_ENV": "production",
        # required or per-release: set on Render, never in this file
        "ATUS_DATABASE_URL": "", "ATUS_EDGE_SECRETS": "", "ATUS_EXPECTED_DATA_VERSION": "",
        "ATUS_OPS_TOKEN": "",
        "WEB_CONCURRENCY": "1",
        "ATUS_LOG_LEVEL": "INFO", "ATUS_LOG_FORMAT": "json", "ATUS_API_DOCS": "off",
        "ATUS_API_CORS_ORIGINS": "", "ATUS_API_CACHE_SIZE": "256",
        "ATUS_API_POOL_MAX_SIZE": "4", "ATUS_API_POOL_MAX_IDLE_S": "240",
        "ATUS_API_POOL_TIMEOUT_S": "5", "ATUS_API_CONNECT_TIMEOUT_S": "10",
        "ATUS_API_STATEMENT_TIMEOUT_MS": "85000", "ATUS_API_META_STATEMENT_TIMEOUT_MS": "10000",
        "ATUS_API_JIT": "off", "ATUS_API_MAX_INFLIGHT": "2", "ATUS_API_MAX_INFLIGHT_HEAVY": "1",
        "ATUS_API_ADMISSION_WAIT_S": "5", "ATUS_API_RATE_ANALYSIS": "6/60",
        "ATUS_API_RATE_REQUESTS": "120/60", "ATUS_API_DAILY_ANALYSIS_CAP": "600",
        "ATUS_API_MAX_BODY_BYTES": "65536", "ATUS_API_THREADS": "16",
        "ATUS_API_SNAPSHOT_TTL_S": "600",
    }
    commented = dict(re.findall(r"^# ([A-Z][A-Z0-9_]*)=(\S*)$", _read(".env.production.example"),
                                re.MULTILINE))
    # unset: the server default (ADR-012 "Calibration (§13)"); set by Render; baked in
    # at build time
    assert commented["ATUS_API_MAX_PARALLEL_WORKERS"] == ""
    assert commented["PORT"] == "10000"
    assert commented["ATUS_BUILD_SHA"] == "dev"
    # documented, never active: the local stack's TLS escape hatch is not Render's
    assert commented["ATUS_DATABASE_TLS"] == ""


def test_production_example_database_url_shape_passes_the_tls_rule():
    """The documented production DSN shape is what production accepts; the
    weaker `require` the template warns about is refused."""
    from atus_pipeline.config import ConfigError, load_settings

    text = _read(".env.production.example")
    shape = re.search(r"^#\s+(postgresql://atus_api:<password>@\S+)$", text, re.MULTILINE)
    url = shape.group(1).replace("<password>", "pw").replace("<id>", "x")
    env = {"ATUS_ENV": "production", "ATUS_EDGE_SECRETS": "e" * 40, "ATUS_DATABASE_URL": url}
    assert load_settings(env).database_tls == "verify"
    with pytest.raises(ConfigError, match="sslmode=require"):
        load_settings({**env, "ATUS_DATABASE_URL": url.replace("verify-full", "require")})


# ADR-012 "Calibration (§13)": the values the 2026-10-21 emulation runs confirmed
# (loadtests/results/2026-10-21-emulation.md, sections 8, 9 and 13).
CALIBRATED = {
    "ATUS_API_STATEMENT_TIMEOUT_MS": "85000", "ATUS_API_MAX_INFLIGHT": "2",
    "ATUS_API_MAX_INFLIGHT_HEAVY": "1", "ATUS_API_ADMISSION_WAIT_S": "5",
}


def test_calibrated_limits_are_the_defaults_and_agree_across_components():
    """The production template, the code's production defaults, the API role's default,
    the request deadline and the edge governor all carry the calibrated values."""
    from atus_pipeline.api.database import DEADLINE_MARGIN_S
    from atus_pipeline.api.protection import HEAVY_YEAR_QUERIES
    from atus_pipeline.config import load_settings
    from atus_pipeline.serving.restore import EXPECTED_ROLE_SETTINGS

    example = _assignments(_read(".env.production.example"))
    assert {name: example[name] for name in CALIBRATED} == CALIBRATED
    settings = load_settings({
        "ATUS_ENV": "production", "ATUS_EDGE_SECRETS": "e" * 40,
        "ATUS_DATABASE_URL": "postgresql://atus_api:not-a-secret@db.invalid:5432/atus"
                             "?sslmode=verify-full&sslrootcert=system",
    })
    assert (settings.api_statement_timeout_ms, settings.api_max_inflight,
            settings.api_max_inflight_heavy, settings.api_admission_wait_s) == (85_000, 2, 1, 5)
    # Unset in production: 0 loses at 2 CU, so ADR-011's rule does not adopt it.
    assert settings.api_max_parallel_workers is None
    assert HEAVY_YEAR_QUERIES == 8

    # The API role's backstop equals the per-transaction value.
    roles = dict(re.findall(r"ALTER ROLE :\"api_role\" SET (\w+) = '?(\w+)'?;",
                            _read("scripts/ops/roles.sql")))
    expected_role_timeout = f"{settings.api_statement_timeout_ms // 1000}s"
    assert roles["statement_timeout"] == EXPECTED_ROLE_SETTINGS["statement_timeout"]
    assert roles["statement_timeout"] == expected_role_timeout

    # The request deadline stays at or under ADR-011's 100 s, and the governor charges
    # exactly it as an admitted analysis's provisional DB-busy time, with the same rule
    # for what is heavy.
    deadline_ms = settings.api_statement_timeout_ms + DEADLINE_MARGIN_S * 1000
    assert deadline_ms <= 100_000
    constants = _read("edge/src/constants.ts")
    busy = re.search(r"^export const PROVISIONAL_BUSY_MS = ([\d_]+)$", constants, re.MULTILINE)
    heavy = re.search(r"^export const HEAVY_YEAR_QUERIES = (\d+)$", constants, re.MULTILINE)
    assert int(busy.group(1).replace("_", "")) == deadline_ms
    assert int(heavy.group(1)) == HEAVY_YEAR_QUERIES


def test_production_example_documents_every_variable_the_code_reads():
    """Any ATUS_* name in src/ must be documented (catches new settings)."""
    names = set()
    for path in (REPO_ROOT / "src").rglob("*.py"):
        names.update(re.findall(r"[\"'](ATUS_[A-Z0-9_]+)[\"']", path.read_text()))
    text = _read(".env.production.example")
    missing = sorted(n for n in names if not re.search(rf"\b{n}=", text))
    assert missing == []


def test_production_example_names_the_edge_and_github_settings():
    text = _read(".env.production.example")
    for name in WORKER_SECRETS + WORKER_VARS + GITHUB_SECRETS:
        assert name in text, name


def test_production_example_names_only_worker_vars_the_worker_reads():
    """Section 3 lists Worker vars; each must be declared in wrangler.jsonc and read by
    the Worker. The Vercel fallback's vars were never implemented (ADR-011 amendment)."""
    text = _read(".env.production.example")
    section = text[text.index("# 3. Cloudflare Worker vars"):text.index("# 4. GitHub")]
    named = set(re.findall(r"^# ([A-Z][A-Z0-9_]*)=", section, re.MULTILINE))
    assert set(WORKER_VARS) <= named
    assert named <= wrangler_vars() & worker_env_names(), named - wrangler_vars()
    for dead in ("API_ORIGIN", "GATE_INFLIGHT", "origin mode"):
        assert dead not in text, dead


def test_production_example_holds_no_secret_values():
    values = _assignments(_read(".env.production.example"))
    for name in ("ATUS_DATABASE_URL", "ATUS_EDGE_SECRETS", "ATUS_OPS_TOKEN",
                 "ATUS_EXPECTED_DATA_VERSION"):
        assert values[name] == "", name
    assert values["ATUS_ENV"] == "production"


def test_stack_example_holds_no_secret_values():
    values = _assignments(_read("deploy/stack/stack.env.example"))
    secrets = [n for n in values if "PASSWORD" in n or "SECRET" in n or "TOKEN" in n
               or "HMAC" in n]
    assert len(secrets) == 7
    assert all(values[n] == "" for n in secrets)


def test_development_example_keeps_development_defaults():
    values = _assignments(_read(".env.example"))
    assert values["ATUS_ENV"] == "development"
    assert values["ATUS_DATABASE_URL"] == "postgresql://atus:atus@localhost:5434/atus"


# --- docs/deployment.md "Configuration reference" (brief §18) -----------------------


def _configuration_reference() -> str:
    text = _read("docs/deployment.md")
    start = text.index("## Configuration reference")
    return text[start:text.index("\n## ", start + 1)]


def test_configuration_reference_covers_every_template_variable():
    """Everything the templates name (active or commented) has a documented row."""
    reference = _configuration_reference()
    names = set()
    for template in (".env.production.example", "deploy/stack/stack.env.example"):
        names.update(re.findall(r"^#? ?([A-Z][A-Z0-9_]+)=", _read(template), re.MULTILINE))
    missing = sorted(n for n in names if n not in reference)
    assert missing == []


def test_configuration_reference_covers_every_component_name():
    reference = _configuration_reference()
    for name in CONTRACT_API_VARIABLES + WORKER_SECRETS + WORKER_VARS + GITHUB_SECRETS:
        assert f"`{name}`" in reference or name in reference, name


def test_configuration_reference_lists_exactly_the_worker_vars():
    """The Worker-var table names every var in wrangler.jsonc and nothing else: no var
    selects the API origin, and the Vercel fallback is not implemented (ADR-011)."""
    reference = _configuration_reference()
    worker = reference[reference.index("### Cloudflare Worker"):reference.index("### GitHub")]
    rows = re.findall(r"^\| `([A-Z][A-Z0-9_]*)`", worker[worker.index("| Var |"):],
                      re.MULTILINE)
    assert len(rows) == len(set(rows))
    assert set(rows) == wrangler_vars()
    for dead in ("API_ORIGIN", "GATE_INFLIGHT", "origin mode"):
        assert dead not in reference, dead


def test_configuration_reference_matches_the_published_openapi_schema():
    """`ATUS_API_DOCS` turns the API's routes off; the site serves the committed schema
    statically instead (F43), so the reference says where it is published."""
    row = next(line for line in _configuration_reference().splitlines()
               if line.startswith("| `ATUS_API_DOCS` |"))
    assert "static asset at `/openapi.json`" in row
    assert "`frontend/openapi.json`" in row


def _workflow_credentials() -> set[str]:
    """Every credential a workflow reads as `secrets.NAME` (identifiers such as account
    ids and the origin URL excluded; GITHUB_TOKEN is the run's own)."""
    names: set[str] = set()
    for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        names.update(re.findall(r"secrets\.([A-Z][A-Z0-9_]*)", path.read_text()))
    return {name for name in names - {"GITHUB_TOKEN"}
            if re.search(r"TOKEN|KEY|HOOK|SECRET|PASSWORD", name)}


def test_every_secret_has_a_rotation_entry():
    """The list is the API's and the Worker's secrets plus every credential the
    workflows read, so a secret added to a workflow (review round 2: the optional
    analytics token) fails here until it has a procedure."""
    reference = _configuration_reference()
    rotation = reference[reference.index("### Rotation"):]
    credentials = _workflow_credentials()
    assert {"CLOUDFLARE_API_TOKEN", "RENDER_DEPLOY_HOOK_URL", "EDGE_OPS_TOKEN"} <= credentials
    for secret in sorted({"ATUS_EDGE_SECRETS", "EDGE_AUTH_SECRET", "atus_api", "ATUS_OPS_TOKEN",
                          "OPS_TOKEN", "IP_HMAC_KEY", "NEON_API_KEY"} | credentials):
        assert secret in rotation, secret
