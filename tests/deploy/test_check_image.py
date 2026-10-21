"""deploy/ci/check_image.py: the CI gate for non-root, no build tools and a DB-free health check.

The evaluation logic is tested without Docker. The gate itself runs against
the real image and against deliberately broken images derived from it
(opt-in: ATUS_DOCKER_TESTS=1, see conftest.py), so each FAIL is shown to fire.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys

import pytest

from .conftest import REPO_ROOT

SCRIPT = REPO_ROOT / "deploy" / "ci" / "check_image.py"


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_file_location("check_image", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through it
    spec.loader.exec_module(module)
    return module


def _dockerfile_healthcheck() -> dict:
    """The HEALTHCHECK of the real Dockerfile, as `docker image inspect` reports it."""
    text = (REPO_ROOT / "Dockerfile").read_text().replace("\\\n", " ")
    match = re.search(r"^HEALTHCHECK .*?\bCMD (\[.*\])$", text, re.MULTILINE)
    return {"Test": ["CMD", *json.loads(match.group(1))]}


CLEAN_CONTENTS = {
    "uid": 10001, "gid": 10001, "groups": [10001], "tools": [],
    # The runtime image's real package list (Debian trixie, python:3.12-slim).
    "packages": ["adduser", "apt", "bash", "ca-certificates", "coreutils", "dpkg",
                 "gcc-14-base", "libc6", "libgcc-s1", "libssl3t64", "libstdc++6", "openssl",
                 "perl-base", "tzdata", "zlib1g"],
    "site_entries": ["pip", "pip-25.0.1.dist-info", "psycopg", "uvicorn-0.54.0.dist-info",
                     "typing_extensions.py", "README.txt"],
    "leftovers": [], "archives": [], "writable": [],
}
PID1_NON_ROOT = "Name:\tatus\nUid:\t10001\t10001\t10001\t10001\nGid:\t10001\t10001\t10001\t10001\n"
PID1_ROOT = "Name:\tatus\nUid:\t0\t0\t0\t0\nGid:\t0\t0\t0\t0\n"


# --- user ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user", "ok"),
    [
        ("10001:10001", True),
        ("10001", True),
        ("", False),  # no USER: root
        (None, False),
        ("0", False),
        ("0:0", False),
        ("root", False),  # a name: platforms cannot verify it is not root
        ("atus", False),
        ("10001:0", False),  # root group
        ("10001:atus", False),
    ],
)
def test_user_problem(gate, user, ok):
    assert (gate.user_problem(user) is None) is ok


def test_proc_ids(gate):
    assert gate.proc_ids(PID1_NON_ROOT) == ({10001}, {10001})
    assert gate.proc_ids(PID1_ROOT) == ({0}, {0})
    assert gate.proc_ids("") == (set(), set())


def test_runtime_user_problems(gate):
    assert gate.runtime_user_problems(CLEAN_CONTENTS, PID1_NON_ROOT) == []
    assert len(gate.runtime_user_problems(CLEAN_CONTENTS, PID1_ROOT)) == 1
    assert len(gate.runtime_user_problems(CLEAN_CONTENTS, "")) == 1  # PID 1 unreadable
    root = dict(CLEAN_CONTENTS, uid=0, gid=0, groups=[0])
    assert len(gate.runtime_user_problems(root, PID1_NON_ROOT)) == 1
    in_root_group = dict(CLEAN_CONTENTS, groups=[10001, 0])
    assert len(gate.runtime_user_problems(in_root_group, PID1_NON_ROOT)) == 1
    writable = dict(CLEAN_CONTENTS, writable=["/opt/venv"])
    assert "/opt/venv" in gate.runtime_user_problems(writable, PID1_NON_ROOT)[0]


# --- build tools --------------------------------------------------------------------


def test_the_runtime_package_list_is_clean(gate):
    assert gate.build_packages(CLEAN_CONTENTS["packages"]) == []
    assert gate.build_tool_problems(CLEAN_CONTENTS) == []


@pytest.mark.parametrize(
    "package",
    ["gcc", "gcc-14", "g++-14", "cpp", "cpp-14", "gcc-14-x86-64-linux-gnu",
     "cpp-14-aarch64-linux-gnu", "clang-19", "build-essential", "make", "binutils",
     "binutils-x86-64-linux-gnu", "libc6-dev", "libpq-dev", "python3.12-dev",
     "linux-libc-dev", "git", "curl", "pkgconf"],
)
def test_build_packages_are_found(gate, package):
    assert gate.build_packages(["libc6", package]) == [package]


@pytest.mark.parametrize("package", ["gcc-14-base", "libgcc-s1", "libstdc++6", "libcurl4t64",
                                     "libgit2-1.9", "makedev", "cpio"])
def test_runtime_support_packages_are_not_build_tools(gate, package):
    assert gate.build_packages([package]) == []


@pytest.mark.parametrize(
    ("entry", "backend"),
    [
        ("setuptools", "setuptools"), ("setuptools-75.8.0.dist-info", "setuptools"),
        ("_distutils_hack", "_distutils_hack"), ("wheel-0.45.1.dist-info", "wheel"),
        ("Cython", "cython"), ("hatchling-1.27.0.dist-info", "hatchling"),
        ("flit_core-3.10.1.dist-info", "flit_core"),
    ],
)
def test_build_backends_are_found(gate, entry, backend):
    assert gate.build_backends(["pip", entry]) == [backend]


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"tools": ["gcc", "make"]}, "on PATH: gcc, make"),
        ({"packages": ["libc6", "gcc-14"]}, "packages: gcc-14"),
        ({"site_entries": ["wheel"]}, "Python build backends: wheel"),
        ({"leftovers": ["/opt/buildenv"]}, "leftovers: /opt/buildenv"),
        ({"archives": ["/tmp/x-1.0-py3-none-any.whl"]}, "wheels/sdists"),
    ],
)
def test_build_tool_problems(gate, change, fragment):
    problems = gate.build_tool_problems({**CLEAN_CONTENTS, **change})
    assert len(problems) == 1 and fragment in problems[0]


def test_contents_probe_runs_and_finds_archives(gate, tmp_path):
    """The in-image probe is plain Python: run it here (on a small tree) to catch
    mistakes early."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "stray-1.0-py3-none-any.whl").write_bytes(b"")
    result = subprocess.run(
        [sys.executable, "-I", "-c", gate.probe_code([str(tmp_path)])],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert set(report) == set(CLEAN_CONTENTS)
    assert report["archives"] == [str(tmp_path / "pkg" / "stray-1.0-py3-none-any.whl")]


# --- health check -------------------------------------------------------------------


def test_the_dockerfile_healthcheck_passes_the_static_check(gate):
    health = _dockerfile_healthcheck()
    assert gate.healthcheck_problem(health) is None
    assert gate.runnable_healthcheck(health)
    assert gate.healthcheck_command(health)[0] == "python"


@pytest.mark.parametrize(
    ("test", "fragment"),
    [
        (None, "no HEALTHCHECK"),
        (["NONE"], "disables"),
        (["CMD"], "unrecognised"),
        (["CMD", "curl", "http://127.0.0.1:10000/ready"], "does not request /health"),
        (["CMD", "python", "-c", "urlopen('http://api:10000/health')"], "loopback"),
        (["CMD-SHELL", "pg_isready -h db && curl -f http://localhost:10000/health"],
         "pg_isready"),
        (["CMD", "python", "-c", "urlopen('http://127.0.0.1:10000/health');"
                                 "urlopen('http://127.0.0.1:10000/api/v1/meta')"], "/api/"),
    ],
)
def test_healthcheck_problems(gate, test, fragment):
    health = None if test is None else {"Test": test}
    problem = gate.healthcheck_problem(health)
    assert problem is not None and fragment in problem


def test_shell_form_healthcheck_runs_through_sh(gate):
    health = {"Test": ["CMD-SHELL", "wget -q http://127.0.0.1:10000/health"]}
    assert gate.healthcheck_command(health) == [
        "/bin/sh", "-c", "wget -q http://127.0.0.1:10000/health"
    ]
    assert not gate.runnable_healthcheck({"Test": ["NONE"]})
    assert not gate.runnable_healthcheck(None)


LOG = "\n".join([
    "2026-10-20 21:00:00.001 UTC [70] user=postgres db=postgres LOG:  statement: CREATE ROLE",
    "2026-10-20 21:00:01.002 UTC [81] user=[unknown] db=[unknown] LOG:  connection received",
    "2026-10-20 21:00:01.003 UTC [81] user=atus_api db=postgres LOG:  connection authorized",
    "2026-10-20 21:00:02.004 UTC [81] user=atus_api db=postgres LOG:  statement: SELECT 1",
    "2026-10-20 21:00:02.005 UTC [90] user=atus_api_admin db=postgres LOG:  statement: SELECT 2",
])


def test_role_lines_belong_to_the_api_role_only(gate):
    lines = gate.role_lines(LOG)
    assert len(lines) == 2
    assert all(" user=atus_api " in line for line in lines)
    assert "connection authorized" in gate.summarize(lines)
    assert gate.summarize(lines, limit=1).endswith("(+1 more)")


@pytest.mark.parametrize("edge_auth", [False, True])
@pytest.mark.parametrize("strict", [False, True])
def test_http_snippets_compile_and_carry_no_secret(gate, edge_auth, strict):
    code = gate.http_get_snippet("/health", edge_auth=edge_auth, strict=strict)
    compile(code, "<snippet>", "exec")
    assert ("ATUS_EDGE_SECRETS" in code) is edge_auth  # read in the container, not passed
    assert ("raise" in code) is strict


def test_the_gates_database_urls_pass_the_production_rules(gate):
    """The gate runs the image in production mode: both API containers must be
    configured so that the image starts (TLS verification, or the local-stack
    escape hatch for the throwaway Postgres reachable as `db`)."""
    from atus_pipeline.config import ConfigError, check_api_settings, load_settings

    base = {"ATUS_ENV": "production", "ATUS_EDGE_SECRETS": "e" * 32}
    check_api_settings(load_settings({**base, "ATUS_DATABASE_URL": gate.UNRESOLVABLE_DATABASE_URL}))
    stack_url = f"postgresql://{gate.API_ROLE}@db:5432/postgres"
    check_api_settings(load_settings({**base, "ATUS_DATABASE_URL": stack_url,
                                      **gate.LOCAL_STACK_TLS}))
    with pytest.raises(ConfigError, match="sslmode"):
        load_settings({**base, "ATUS_DATABASE_URL": stack_url})


def test_stack_redacts_edge_secrets(gate):
    stack = gate.Stack("image", "prefix", "postgres", timeout=1)
    stack.edge_secrets.append("s3cr3t-value")
    assert stack.redact("ATUS_EDGE_SECRETS=s3cr3t-value bad") == "ATUS_EDGE_SECRETS=*** bad"


def test_static_results(gate):
    config = {"User": "10001:10001", "Healthcheck": _dockerfile_healthcheck()}
    results = gate.static_checks(config, CLEAN_CONTENTS)
    assert [(r.name, r.status) for r in results] == [
        ("user", "PASS"), ("build_tools", "PASS"), ("healthcheck", "PASS")
    ]
    results = gate.static_checks({"User": ""}, {**CLEAN_CONTENTS, "tools": ["gcc"]})
    assert [r.status for r in results] == ["FAIL", "FAIL", "FAIL"]


def test_report_exit_codes(gate, capsys):
    passing = [gate.Result("user", "PASS", "ok"), gate.Result("x", "SKIP", "later")]
    assert gate.report(passing) == 0
    assert gate.report([*passing, gate.Result("build_tools", "FAIL", "gcc")]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] build_tools" in out and "image check: 1 failed" in out


# --- the gate against real images (opt-in) ------------------------------------------


def _gate(image: str, prefix: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), image, "--name-prefix", f"{prefix}-imagecheck",
         "--timeout", "120", "--health-runs", "2", *args],
        capture_output=True, text=True, timeout=900, check=False,
    )


def _statuses(output: str) -> dict[str, str]:
    found = re.findall(r"^\[(PASS|FAIL|SKIP)\] (\S+)", output, re.MULTILINE)
    return {name: status for status, name in found}


@pytest.fixture(scope="module")
def derived_image(api_image, docker_prefix):
    """Build a throwaway image FROM the API image with extra Dockerfile lines."""
    built = []

    def build(name: str, lines: str) -> str:
        tag = f"{docker_prefix}-imagecheck-{name}:test"
        result = subprocess.run(
            ["docker", "build", "-q", "-t", tag, "-"],
            input=f"FROM {api_image}\n{lines}\n", capture_output=True, text=True,
            timeout=300, check=False,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        built.append(tag)
        return tag

    yield build
    for tag in built:
        subprocess.run(["docker", "image", "rm", "-f", tag], capture_output=True, check=False)


def _assert_no_leftovers(prefix: str) -> None:
    names = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={prefix}-imagecheck", "--format",
         "{{.Names}}"], capture_output=True, text=True, check=False,
    ).stdout.split()
    networks = subprocess.run(
        ["docker", "network", "ls", "--filter", f"name={prefix}-imagecheck", "--format",
         "{{.Name}}"], capture_output=True, text=True, check=False,
    ).stdout.split()
    assert names == [] and networks == []


def test_api_image_passes_the_gate(api_image, docker_prefix):
    """Strict, with no startup allowance: the pool is lazy (ADR-011 min_size=0)."""
    result = _gate(api_image, docker_prefix)
    assert result.returncode == 0, result.stdout + result.stderr[-3000:]
    statuses = _statuses(result.stdout)
    for name in ("user", "build_tools", "healthcheck", "health_no_db", "user_runtime",
                 "health_db_quiet", "detector", "startup_db_quiet"):
        assert statuses[name] == "PASS", name
    _assert_no_leftovers(docker_prefix)


def test_api_image_opens_no_database_connection_at_start(api_image, docker_prefix):
    result = _gate(api_image, docker_prefix)
    assert result.returncode == 0, result.stdout
    assert _statuses(result.stdout)["startup_db_quiet"] == "PASS"


def test_root_user_and_build_tools_fail_the_gate(derived_image, docker_prefix):
    image = derived_image(
        "root-tools", "USER 0\nRUN ln -s /bin/true /usr/local/bin/gcc && mkdir /opt/buildenv"
    )
    result = _gate(image, docker_prefix)
    assert result.returncode == 1, result.stdout + result.stderr[-3000:]
    statuses = _statuses(result.stdout)
    assert statuses["user"] == statuses["user_runtime"] == statuses["build_tools"] == "FAIL"
    assert "on PATH: gcc" in result.stdout and "/opt/buildenv" in result.stdout
    assert statuses["health_db_quiet"] == "PASS"  # only what is broken fails


def test_a_health_check_that_queries_the_database_fails_the_gate(derived_image, docker_prefix):
    """The static check cannot see this one (the path is assembled at run time);
    the database log can."""
    probe = (
        "import os, urllib.request as u\\n"
        "u.urlopen('http://127.0.0.1:10000/health')\\n"
        "try:\\n"
        "    u.urlopen(u.Request('http://127.0.0.1:10000/a' + 'pi/v1/meta', headers="
        "{'X-ATUS-Edge-Auth': os.environ['ATUS_EDGE_SECRETS']}), timeout=4)\\n"
        "except Exception:\\n"
        "    pass\\n"
    )
    image = derived_image(
        "db-health",
        "HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --start-interval=2s "
        f'CMD ["python", "-c", "{probe}"]',
    )
    result = _gate(image, docker_prefix)
    assert result.returncode == 1, result.stdout + result.stderr[-3000:]
    statuses = _statuses(result.stdout)
    assert statuses["healthcheck"] == "PASS"
    assert statuses["health_db_quiet"] == "FAIL"
    assert statuses["detector"] == "PASS"
    _assert_no_leftovers(docker_prefix)


def test_unknown_image_is_an_error_not_a_failure(docker_daemon, docker_prefix):
    result = _gate(f"{docker_prefix}-imagecheck-missing:never", docker_prefix)
    assert result.returncode == 2
    assert "not found locally" in result.stderr
