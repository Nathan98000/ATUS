"""``sslrootcert=system`` on the owner's machine, outside the image (opt-in:
``ATUS_DOCKER_TESTS=1``; needs the ``postgres:16`` image).

The restore (owner step 3.5) and the local API smoke (Data release step 5)
connect to Neon with ``sslmode=verify-full&sslrootcert=system`` from the
owner's Mac: from this Python environment, whose ``psycopg[binary]`` OpenSSL
has no usable default roots, and through ``pg_restore``/``psql`` from the
``postgres:16`` image when no local client is new enough, an image with no
CA bundle at all. A throwaway CA stands in for the operating system's
bundle, and a TLS Postgres with a certificate for ``localhost`` and
``host.docker.internal`` stands in for Neon.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import secrets
import subprocess
import time

import psycopg
import pytest

from atus_pipeline.database import dsn
from atus_pipeline.serving import pgtools

from .test_container_image import POSTGRES_IMAGE, docker

SSL_OF_THIS_SESSION = "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
REAL_BUNDLE = dsn.system_ca_bundle()      # before any test replaces the list


@pytest.fixture(scope="module")
def owner_certificates(tmp_path_factory):
    """A throwaway CA and a server certificate for this machine's names."""
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ATUS throwaway owner CA")])
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
    names = [x509.DNSName("localhost"), x509.DNSName("host.docker.internal"),
             x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(hours=1)).not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .sign(ca_key, hashes.SHA256())
    )
    directory = tmp_path_factory.mktemp("owner-tls")
    pem = serialization.Encoding.PEM
    (directory / "ca.crt").write_bytes(ca_cert.public_bytes(pem))
    (directory / "server.crt").write_bytes(server_cert.public_bytes(pem))
    (directory / "server.key").write_bytes(
        server_key.private_bytes(pem, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption())
    )
    return directory


@pytest.fixture(scope="module")
def tls_only_postgres(docker_daemon, docker_prefix, owner_certificates):
    """A Postgres that accepts TLS connections only, published on 127.0.0.1;
    yields the owner's ``verify-full`` + ``system`` connection string."""
    name = f"{docker_prefix}-owner-tls-{secrets.token_hex(4)}"
    password = secrets.token_hex(16)
    start = (
        "install -m 600 -o postgres -g postgres /certs/server.key /tmp/server.key && "
        "install -m 644 -o postgres -g postgres /certs/server.crt /tmp/server.crt && "
        "printf 'local all all trust\\nhostssl all all all scram-sha-256\\n' > /tmp/hba.conf && "
        "chown postgres /tmp/hba.conf && "
        "exec docker-entrypoint.sh postgres -c ssl=on -c hba_file=/tmp/hba.conf "
        "-c ssl_cert_file=/tmp/server.crt -c ssl_key_file=/tmp/server.key"
    )
    docker(
        "run", "-d", "--name", name, "-p", "127.0.0.1::5432",
        "-e", f"POSTGRES_PASSWORD={password}",
        "-v", f"{owner_certificates}:/certs:ro",
        "--entrypoint", "bash", POSTGRES_IMAGE, "-c", start,
    )
    try:
        for _ in range(120):
            if docker("exec", name, "pg_isready", "-h", "127.0.0.1", check=False).returncode == 0:
                break
            time.sleep(0.5)
        else:
            pytest.fail("TLS Postgres did not become ready")
        port = docker("port", name, "5432").stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        yield (f"postgresql://postgres:{password}@localhost:{port}/postgres"
               "?sslmode=verify-full&sslrootcert=system")
    finally:
        docker("rm", "-f", name, check=False)


@pytest.fixture
def owner_machine(monkeypatch, owner_certificates):
    """This machine with the throwaway CA as its only system bundle and no
    SSL_CERT_FILE, like the owner's Mac before any `atus` command runs."""
    monkeypatch.setattr(dsn, "SYSTEM_CA_BUNDLES", (str(owner_certificates / "ca.crt"),))
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    return owner_certificates


def _ssl_through_psql(url: str) -> subprocess.CompletedProcess:
    tool = pgtools.resolve("psql", url, server_major=16, prefer_docker=True,
                           image=POSTGRES_IMAGE)
    return tool.run(["-X", "-tAc", SSL_OF_THIS_SESSION])


def test_the_hosts_psycopg_verifies_against_the_system_bundle(tls_only_postgres, owner_machine):
    assert dsn.ensure_system_roots(tls_only_postgres) == str(owner_machine / "ca.crt")
    with psycopg.connect(tls_only_postgres, connect_timeout=15) as conn:
        assert conn.execute(SSL_OF_THIS_SESSION).fetchone()[0] is True


def test_the_docker_client_verifies_against_the_hosts_bundle(tls_only_postgres, owner_machine):
    result = _ssl_through_psql(tls_only_postgres)
    assert result.stdout.decode().strip() == "t"


def test_the_docker_client_still_rejects_an_unknown_ca(tls_only_postgres, owner_machine,
                                                       monkeypatch):
    # The machine's real bundle does not know the throwaway CA: verification
    # stays on in the container.
    if REAL_BUNDLE is None:
        pytest.skip("no system CA bundle on this machine")
    monkeypatch.setenv("SSL_CERT_FILE", REAL_BUNDLE)
    with pytest.raises(pgtools.PgToolError, match="certificate verify failed"):
        _ssl_through_psql(tls_only_postgres)


def test_without_any_bundle_the_docker_client_refuses_with_advice(tls_only_postgres,
                                                                  monkeypatch):
    monkeypatch.setattr(dsn, "SYSTEM_CA_BUNDLES", ())
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    with pytest.raises(pgtools.PgToolError, match="SSL_CERT_FILE"):
        _ssl_through_psql(tls_only_postgres)
