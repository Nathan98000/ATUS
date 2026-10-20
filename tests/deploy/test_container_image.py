"""The API image, run as a container (opt-in: ATUS_DOCKER_TESTS=1; see conftest.py).

Functional checks only, no timings:
- it runs as the unprivileged user and contains no tests, data, migrations,
  build tools or pipeline-only packages;
- libpq is >= 16 and `sslmode=verify-full sslrootcert=system` really verifies
  the server certificate against the image's CA bundle (ADR-011 "TLS"), shown
  against a throwaway TLS Postgres with a throwaway CA;
- /health answers with no reachable database, and Docker's health check
  turns healthy (liveness never touches the DB);
- production mode refuses to start without its required settings (F44).
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import subprocess
import time
import urllib.request

import pytest

POSTGRES_IMAGE = (
    "postgres:16@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94"
)
UNREACHABLE_DB = "postgresql://atus_api:unused@127.0.0.1:1/atus"


def docker(*args: str, check: bool = True, timeout: float = 300) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False, timeout=timeout
    )
    if check:
        assert result.returncode == 0, f"docker {args[0]}: {result.stderr[-2000:]}"
    return result


def run_python(image: str, code: str, *docker_args: str, unset: str | None = None):
    """Run ``python -c code`` in the image; optionally with one variable unset."""
    command = ["run", "--rm", *docker_args]
    if unset:
        command += ["--entrypoint", "env", image, "-u", unset, "python", "-c", code]
    else:
        command += ["--entrypoint", "python", image, "-c", code]
    return docker(*command, check=False)


@pytest.fixture
def unique() -> str:
    return secrets.token_hex(4)


# --- contents -------------------------------------------------------------------


def test_runs_as_an_unprivileged_user(api_image):
    out = docker("run", "--rm", "--entrypoint", "id", api_image).stdout
    assert out.startswith("uid=10001(atus) gid=10001(atus)")


def test_contains_only_the_runtime(api_image):
    code = """
import importlib.util, json, os, pathlib
import atus_pipeline
pkg = pathlib.Path(atus_pipeline.__file__).parent
print(json.dumps({
    "modules": {m: importlib.util.find_spec(m) is not None
                for m in ("fastapi", "uvicorn", "psycopg", "psycopg_pool", "curl_cffi",
                          "pytest", "httpx", "pdfplumber", "setuptools", "ruff")},
    "paths": {p: os.path.exists(p)
              for p in ("/build", "/dist", "/opt/buildenv", "/app/.env", "/app/tests",
                        "/app/data", "/app/migrations")},
    "app_dir": sorted(os.listdir("/app")),
    "migrations_in_package": any(pkg.rglob("*.sql")),
    "pyc_present": any(pkg.rglob("*.pyc")),
}))
"""
    result = run_python(api_image, code)
    assert result.returncode == 0, result.stderr
    found = json.loads(result.stdout)
    assert found["modules"] == {
        "fastapi": True, "uvicorn": True, "psycopg": True, "psycopg_pool": True,
        "curl_cffi": False, "pytest": False, "httpx": False, "pdfplumber": False,
        "setuptools": False, "ruff": False,
    }
    assert not any(found["paths"].values()), found["paths"]
    assert found["app_dir"] == []
    assert found["migrations_in_package"] is False
    assert found["pyc_present"] is True  # compiled at build time, not at cold start


def test_labels_and_build_metadata(api_image):
    config = json.loads(docker("image", "inspect", api_image).stdout)[0]["Config"]
    labels = config["Labels"]
    assert labels["org.opencontainers.image.source"] == "https://github.com/Nathan98000/ATUS"
    assert labels["org.opencontainers.image.base.digest"].startswith("sha256:")
    env = dict(item.split("=", 1) for item in config["Env"])
    assert env["ATUS_BUILD_SHA"] == labels["org.opencontainers.image.revision"]
    assert env["ATUS_ENV"] == "production"
    assert config["User"] == "10001:10001"


# --- libpq and TLS ----------------------------------------------------------------


def test_libpq_is_new_enough_for_sslrootcert_system(api_image):
    code = "from psycopg import pq; print(pq.__impl__, pq.version())"
    impl, version = run_python(api_image, code).stdout.split()
    assert impl == "binary"
    assert int(version) >= 160000  # sslrootcert=system needs libpq 16


@pytest.fixture(scope="module")
def tls_certificates(tmp_path_factory):
    """A throwaway CA and a server certificate for the hostname `tlsdb`."""
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ATUS throwaway test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(hours=1)).not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(digital_signature=False, content_commitment=False,
                          key_encipherment=False, data_encipherment=False, key_agreement=False,
                          key_cert_sign=True, crl_sign=True, encipher_only=False,
                          decipher_only=False),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tlsdb")]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(hours=1)).not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("tlsdb")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .sign(ca_key, hashes.SHA256())
    )
    directory = tmp_path_factory.mktemp("tls")
    pem = serialization.Encoding.PEM
    (directory / "ca.crt").write_bytes(ca_cert.public_bytes(pem))
    (directory / "server.crt").write_bytes(server_cert.public_bytes(pem))
    (directory / "server.key").write_bytes(
        server_key.private_bytes(pem, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption())
    )
    return directory


@pytest.fixture(scope="module")
def tls_postgres(docker_daemon, tls_certificates):
    """A TLS-only Postgres reachable as `tlsdb` on a private network; yields (network, pw)."""
    suffix = secrets.token_hex(4)
    network, name = f"atus-wsc-tls-{suffix}", f"atus-wsc-tlsdb-{suffix}"
    password = secrets.token_hex(16)
    docker("network", "create", network)
    try:
        start = (
            "install -m 600 -o postgres -g postgres /certs/server.key /tmp/server.key && "
            "install -m 644 -o postgres -g postgres /certs/server.crt /tmp/server.crt && "
            "exec docker-entrypoint.sh postgres -c ssl=on "
            "-c ssl_cert_file=/tmp/server.crt -c ssl_key_file=/tmp/server.key"
        )
        docker(
            "run", "-d", "--name", name, "--network", network, "--network-alias", "tlsdb",
            "-e", f"POSTGRES_PASSWORD={password}",
            "-v", f"{tls_certificates}:/certs:ro",
            "--entrypoint", "bash", POSTGRES_IMAGE, "-c", start,
        )
        for _ in range(120):
            ready = docker("exec", name, "pg_isready", "-h", "127.0.0.1", check=False)
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            pytest.fail("TLS Postgres did not become ready")
        yield network, password
    finally:
        docker("rm", "-f", name, check=False)
        docker("network", "rm", network, check=False)


TLS_CONNECT = (
    "import psycopg\n"
    "conn = psycopg.connect('host=tlsdb user=postgres dbname=postgres "
    "sslmode=verify-full sslrootcert=system', connect_timeout=15)\n"
    "print('ssl', conn.execute("
    "'SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()').fetchone()[0])\n"
)


def _tls_client(api_image, tls_postgres, tls_certificates, *, trust_test_ca: bool,
                unset: str | None = None):
    network, password = tls_postgres
    args = ["--network", network, "-e", f"PGPASSWORD={password}"]
    if trust_test_ca:
        # The test CA stands in for the public root that signs Neon's certificate.
        args += ["-v", f"{tls_certificates / 'ca.crt'}:/etc/ssl/certs/ca-certificates.crt:ro"]
    return run_python(api_image, TLS_CONNECT, *args, unset=unset)


def test_sslrootcert_system_verifies_against_the_image_ca_bundle(
    api_image, tls_postgres, tls_certificates
):
    result = _tls_client(api_image, tls_postgres, tls_certificates, trust_test_ca=True)
    assert result.returncode == 0, result.stderr[-1500:]
    assert result.stdout.strip() == "ssl True"


def test_sslrootcert_system_rejects_an_untrusted_certificate(
    api_image, tls_postgres, tls_certificates
):
    """With Debian's real bundle the throwaway CA is unknown: verification is on."""
    result = _tls_client(api_image, tls_postgres, tls_certificates, trust_test_ca=False)
    assert result.returncode != 0
    assert "certificate verify failed" in result.stderr


def test_ssl_cert_file_is_what_makes_system_roots_work(api_image, tls_postgres, tls_certificates):
    """The wheel's bundled OpenSSL looks for roots under its build machine's
    directory, not Debian's: without SSL_CERT_FILE even a trusted CA fails."""
    result = _tls_client(
        api_image, tls_postgres, tls_certificates, trust_test_ca=True, unset="SSL_CERT_FILE"
    )
    assert result.returncode != 0
    assert "certificate verify failed" in result.stderr


# --- process behaviour ---------------------------------------------------------------


def test_health_answers_without_a_database(api_image, unique):
    name = f"atus-wsc-health-{unique}"
    docker(
        "run", "-d", "--name", name, "-p", "127.0.0.1::10000",
        "-e", f"ATUS_DATABASE_URL={UNREACHABLE_DB}", "-e", "ATUS_EDGE_SECRETS=test-secret",
        api_image,
    )
    try:
        port = docker("port", name, "10000/tcp").stdout.strip().rsplit(":", 1)[1]
        body = status = None
        for _ in range(120):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
                    status, body = r.status, json.loads(r.read())
                break
            except OSError:
                time.sleep(0.5)
        assert status == 200 and body["status"] == "ok", body
        for _ in range(120):
            health = docker("inspect", "-f", "{{.State.Health.Status}}", name).stdout.strip()
            if health != "starting":
                break
            time.sleep(0.5)
        assert health == "healthy"
    finally:
        docker("rm", "-f", name, check=False)


@pytest.mark.xfail(
    reason="ATUS_ENV=production start-up checks land with WS-A (API workstream); "
    "remove this marker once merged",
    strict=False,
)
def test_production_mode_refuses_to_start_without_its_settings(api_image, unique):
    name = f"atus-wsc-refuse-{unique}"
    try:
        result = subprocess.run(
            ["docker", "run", "--rm", "--name", name, api_image],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the API started without ATUS_DATABASE_URL and ATUS_EDGE_SECRETS")
    finally:
        docker("rm", "-f", name, check=False)
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "ATUS_DATABASE_URL" in output
