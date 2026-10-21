"""deploy/ci/check_image.py: the CI gate for non-root, no build tools and a DB-free health check.

The evaluation logic is tested without Docker. The gate itself runs against
the real image and against deliberately broken images derived from it
(opt-in: ATUS_DOCKER_TESTS=1, see conftest.py), so each FAIL is shown to fire.
"""

from __future__ import annotations

import importlib.util
import json
import os
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


# --- bytecode and start-up imports --------------------------------------------------


@pytest.fixture
def compiled(tmp_path):
    """A source file and its compiled .pyc, in the mode asked for."""
    import py_compile

    def make(mode=py_compile.PycInvalidationMode.TIMESTAMP):
        source = tmp_path / "mod.py"
        source.write_text("VALUE = 1\n")
        cached = tmp_path / "mod.pyc"
        py_compile.compile(str(source), cfile=str(cached), invalidation_mode=mode,
                           doraise=True)
        return source, cached

    return make


def test_pyc_status_timestamp(gate, compiled, tmp_path):
    source, cached = compiled()
    assert gate.pyc_status(str(source), str(cached)) == "ok"
    assert gate.pyc_status(str(source), str(tmp_path / "absent.pyc")) == "missing"
    stat = source.stat()
    os.utime(source, (stat.st_atime, stat.st_mtime + 10))  # a changed source mtime
    assert gate.pyc_status(str(source), str(cached)) == "stale"


def test_pyc_status_size_and_magic(gate, compiled):
    source, cached = compiled()
    stat = source.stat()
    source.write_text("VALUE = 22\n")  # one byte longer, same mtime
    os.utime(source, (stat.st_atime, stat.st_mtime))
    assert gate.pyc_status(str(source), str(cached)) == "stale"
    source, cached = compiled()
    cached.write_bytes(b"\x00\x00\r\n" + cached.read_bytes()[4:])  # another Python's magic
    assert gate.pyc_status(str(source), str(cached)) == "stale"
    cached.write_bytes(b"short")
    assert gate.pyc_status(str(source), str(cached)) == "stale"


def test_pyc_status_hash_based(gate, compiled):
    import py_compile

    source, cached = compiled(py_compile.PycInvalidationMode.CHECKED_HASH)
    assert gate.pyc_status(str(source), str(cached)) == "ok"
    source.write_text("VALUE = 2\n")
    assert gate.pyc_status(str(source), str(cached)) == "stale"
    source, cached = compiled(py_compile.PycInvalidationMode.UNCHECKED_HASH)
    source.write_text("VALUE = 2\n")
    assert gate.pyc_status(str(source), str(cached)) == "ok"  # Python never re-checks it


@pytest.mark.parametrize(
    ("imported", "found"),
    [
        (["atus_pipeline.api.app", "psycopg", "click", "dotenvx"], []),
        (["dotenv", "dotenv.main"], ["dotenv", "dotenv.main"]),
        (["atus_pipeline.loading.loader", "atus_pipeline.serving_cli"],
         ["atus_pipeline.loading.loader"]),  # serving_cli is the CLI module, not serving
        (["curl_cffi.requests"], ["curl_cffi.requests"]),
    ],
)
def test_forbidden_imports(gate, imported, found):
    assert gate.forbidden_imports(imported) == found


def test_startup_checks(gate):
    clean = {"started": True, "error": None,
             "modules": {"json": "ok", "atus_pipeline.api.app": "ok"},
             "imported": ["json", "atus_pipeline.api.app", "sys"]}
    results = gate.startup_checks(clean)
    assert [(r.name, r.status) for r in results] == [("bytecode", "PASS"),
                                                      ("startup_imports", "PASS")]
    assert "all 2 modules" in results[0].detail

    compiled_at_start = {**clean, "modules": {"json": "missing", "ast": "stale", "re": "ok"},
                         "imported": ["json", "ast", "re", "dotenv"]}
    bytecode, imports = gate.startup_checks(compiled_at_start)
    assert bytecode.status == "FAIL" and "2 of 3 modules" in bytecode.detail
    assert "ast (stale)" in bytecode.detail and "json (missing)" in bytecode.detail
    assert imports.status == "FAIL" and imports.detail == "imported at start-up: dotenv"

    many = {**clean, "modules": {f"m{i}": "missing" for i in range(9)}}
    assert "(+3 more)" in gate.startup_checks(many)[0].detail

    not_started = {"started": False, "error": "ConfigError: x", "modules": {}, "imported": []}
    assert [r.status for r in gate.startup_checks(not_started)] == ["FAIL", "FAIL"]
    assert "ConfigError: x" in gate.startup_checks(not_started)[0].detail
    nothing = {**clean, "modules": {}}
    assert gate.startup_checks(nothing)[0].status == "FAIL"


def test_startup_probe_runs_here(gate, tmp_path):
    """The in-image probe is plain Python: run it against this checkout (in
    production mode, as the image runs it) to catch mistakes early. It starts
    the API on a loopback port the OS picks and stops it again."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ATUS_", "PG"))}
    env["ATUS_ENV"] = "production"
    result = subprocess.run(
        [sys.executable, "-c", gate.startup_probe_code(timeout=30)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["started"] is True, report["error"]
    assert {"atus_pipeline.cli", "atus_pipeline.api.app", "uvicorn.server"} <= set(
        report["imported"])
    assert set(report["modules"]) <= set(report["imported"])
    assert set(report["modules"].values()) <= {"ok", "missing", "stale"}
    assert gate.forbidden_imports(report["imported"]) == []
    assert "secrets.token_urlsafe(32)" in gate.startup_probe_code()  # made in the probe


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
    for name in ("user", "build_tools", "healthcheck", "bytecode", "startup_imports",
                 "health_no_db", "user_runtime", "health_db_quiet", "detector",
                 "startup_db_quiet"):
        assert statuses[name] == "PASS", name
    _assert_no_leftovers(docker_prefix)


def test_missing_bytecode_and_pipeline_imports_fail_the_gate(derived_image, docker_prefix):
    """The base image's state before the stdlib was compiled (no .pyc), plus a
    start-up that imports a forbidden module (a .pth line runs at every start)."""
    image = derived_image(
        "cold-start",
        "USER 0\n"
        "RUN find /usr/local/lib/python3.12 -name '*.pyc' -delete \\\n"
        " && echo 'import dotenv' > /opt/venv/lib/python3.12/site-packages/zz_cold.pth\n"
        "USER 10001:10001",
    )
    result = _gate(image, docker_prefix, "--static-only")
    assert result.returncode == 1, result.stdout + result.stderr[-3000:]
    statuses = _statuses(result.stdout)
    assert statuses["bytecode"] == statuses["startup_imports"] == "FAIL"
    assert "have no usable .pyc and are compiled at every start" in result.stdout
    assert "(missing)" in result.stdout
    assert "imported at start-up: dotenv" in result.stdout
    assert statuses["user"] == statuses["build_tools"] == statuses["healthcheck"] == "PASS"
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
