"""Opt-in end-to-end mTLS deployment on a fresh, runner-owned PostgreSQL cluster."""

import base64
import os
import socket
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from psycopg.conninfo import make_conninfo

from scripts import deployment_db as deployment
from scripts import postgres
from scripts.import_catalog import lock_catalog
from scripts.processes import Processes

REVISION = "a" * 40
pytestmark = pytest.mark.integration


def certificate(root, label, cn, *, issuer=None, ca=False, server=False):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    issuer_name, signing_key = (issuer[0].subject, issuer[1]) if issuer else (subject, key)
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(issuer_name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=0 if ca else None), critical=True)
    )
    if not ca:
        purpose = ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH
        builder = builder.add_extension(x509.ExtendedKeyUsage([purpose]), critical=False)
    cert = builder.sign(signing_key, hashes.SHA256())
    cert_path, key_path = root / f"{label}.crt", root / f"{label}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    key_path.chmod(0o600)
    return cert, key, cert_path, key_path


@pytest.fixture
def tls_database(tmp_path, request, monkeypatch):
    pg_bin = postgres.find_binaries(request.config.getoption("--pg-bin"))
    for name in list(os.environ):
        if name.upper().startswith("PG"):
            monkeypatch.delenv(name)
    ca = certificate(tmp_path, "ca", "deployment-test-ca", ca=True)
    # Deliberately unrelated CN and no SAN: verify-ca must work via 127.0.0.1.
    server = certificate(tmp_path, "server", "unrelated-server-identity", issuer=ca, server=True)
    admin = certificate(tmp_path, "admin", "postgres", issuer=ca)
    importer = certificate(tmp_path, "importer", "dl_importer", issuer=ca)
    rogue = certificate(tmp_path, "untrusted", "postgres")
    data = tmp_path / "data"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    processes = Processes(tmp_path / "logs")
    try:
        processes.run_command("initdb", [
            str(postgres.binary(pg_bin, "initdb")), "-D", str(data), "-U", "postgres",
            "--auth=trust", "--encoding=UTF8", "--locale=C",
        ])

        def config_path(path):
            return str(path).replace("\\", "/").replace("'", "''")

        with (data / "postgresql.conf").open("a", encoding="utf-8") as stream:
            stream.write(
                "\nlisten_addresses = '127.0.0.1'\nunix_socket_directories = ''\nssl = on\n"
                f"port = {port}\nssl_cert_file = '{config_path(server[2])}'\n"
                f"ssl_key_file = '{config_path(server[3])}'\n"
                f"ssl_ca_file = '{config_path(ca[2])}'\n"
            )
        (data / "pg_hba.conf").write_text(
            "hostssl all postgres 127.0.0.1/32 cert\n"
            "hostssl draft dl_importer 127.0.0.1/32 cert\n"
            "host all all 0.0.0.0/0 reject\n",
            encoding="utf-8",
        )
        options = deployment.connection_options({
            "PGHOST": "127.0.0.1", "PGPORT": str(port), "PGSSLMODE": "verify-ca",
            "PGSSLROOTCERT": str(ca[2]), "PGSSLCERT": str(admin[2]), "PGSSLKEY": str(admin[3]),
        })
        child = processes.start("postgres", [
            str(postgres.binary(pg_bin, "postgres")), "-D", str(data),
        ])
        postgres.wait_database(child, make_conninfo(**options, dbname="postgres", user="postgres"))
        yield options, importer, rogue
    finally:
        processes.close()


def test_fresh_tls_setup_idempotence_role_limits_and_serialized_import(tls_database, tmp_path):
    options, importer, rogue = tls_database
    deployment.verify(options, "test", REVISION, rogue[2], rogue[3])
    # An unmarked database is never overwritten, even if it is empty.
    with psycopg.connect(**options, dbname="postgres", user="postgres", autocommit=True) as conn:
        conn.execute("CREATE DATABASE draft TEMPLATE template0")
        with pytest.raises(RuntimeError, match="unmarked or partially"):
            deployment.initialize(options, "test", REVISION, "integration-secret")
        conn.execute("DROP DATABASE draft")
    assert deployment.initialize(options, "test", REVISION, "integration-secret")
    assert not deployment.initialize(options, "test", "b" * 40, "integration-secret")
    with pytest.raises(RuntimeError, match="does not match"):
        deployment.initialize(options, "different", REVISION, "integration-secret")

    importer_options = {**options, "sslcert": str(importer[2]), "sslkey": str(importer[3])}
    image = tmp_path / "pokemon.png"
    image.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg=="
    ))
    source = tmp_path / "catalog.csv"
    source.write_text("dex,species,form,points,image_path\n25,Pikachu,,10,pokemon.png\n")
    assert deployment.import_data(importer_options, "test", "b" * 40, source, tmp_path) == (1, 1)
    with psycopg.connect(**options, dbname="draft", user="postgres") as conn:
        assert conn.execute(
            "SELECT revision FROM dl_deployment.completion"
        ).fetchone() == (REVISION,)
        assert conn.execute(
            "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
            "rolbypassrls, rolinherit FROM pg_roles WHERE rolname = 'dl_importer'"
        ).fetchone() == (True, False, False, False, False, False, False)
        assert conn.execute(
            "SELECT set_option, inherit_option, admin_option FROM pg_auth_members "
            "WHERE roleid = 'dl_owner'::regrole AND member = 'dl_importer'::regrole"
        ).fetchone() == (True, False, False)
        lock_catalog(conn)
        with pytest.raises(RuntimeError, match="Another database initialization or catalog import"):
            deployment.import_data(importer_options, "test", REVISION, source, tmp_path)
        with pytest.raises(RuntimeError, match="Another database initialization or catalog import"):
            deployment.initialize(options, "test", REVISION, "integration-secret")
    with psycopg.connect(**importer_options, dbname="draft", user="dl_importer") as conn:
        conn.execute("SET ROLE dl_owner")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM dl_deployment.completion")
    deployment.verify(options, "test", REVISION, rogue[2], rogue[3])
