import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from host import bootstrap


@pytest.fixture
def host_config():
    return {
        "name": "draft",
        "hostname": "draft.example.com",
        "callback_path": "/g",
        "uv_version": "0.12.20",
        "node_version": "24.22.0",
        "app_environment": {
            "DATABASE_URL": "postgresql://dl_gateway:private-password@127.0.0.1:5432/draft",
            "SESSION_SECRET": "s" * 40,
            "PUBLIC_BASE_URL": "https://draft.example.com",
            "APP_ENV": "production",
            "COOKIE_SECURE": "true",
            "OIDC_ISSUER": "https://accounts.google.com",
            "OIDC_CALLBACK_PATH": "/g",
            "OIDC_CLIENT_ID": "client-id",
            "OIDC_CLIENT_SECRET": "private-client-secret",
        },
    }


def certificates(directory, *, expired=False, client_only=False):
    directory.mkdir(parents=True)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "private deployment CA")])
    now = datetime.now(timezone.utc)
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=30))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    server = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "database-server")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now + timedelta(days=-1 if expired else 2))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([
                ExtendedKeyUsageOID.CLIENT_AUTH if client_only else ExtendedKeyUsageOID.SERVER_AUTH
            ]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    (directory / "ca.crt").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (directory / "server.crt").write_bytes(server.public_bytes(serialization.Encoding.PEM))
    (directory / "server.key").write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    return directory


def test_server_certificate_validation_does_not_require_address_or_dns(tmp_path):
    directory = certificates(tmp_path / "tls")
    result = bootstrap.validate_tls_source(directory)
    assert set(result) == {"server.key", "server.crt", "ca.crt"}


@pytest.mark.parametrize("fault", ["expired", "client_only", "wrong_key", "wrong_ca", "extra_key"])
def test_invalid_server_material_fails_before_installation(tmp_path, fault):
    directory = certificates(
        tmp_path / "tls", expired=fault == "expired", client_only=fault == "client_only"
    )
    if fault in {"wrong_key", "wrong_ca"}:
        other = certificates(tmp_path / "other")
        filename = "server.key" if fault == "wrong_key" else "ca.crt"
        (directory / filename).write_bytes((other / filename).read_bytes())
    elif fault == "extra_key":
        (directory / "ca.key").write_bytes(b"must remain on workstation")
    with pytest.raises(RuntimeError, match="TLS|tls"):
        bootstrap.validate_tls_source(directory)


@pytest.mark.parametrize("port", [":5433", ":6543"])
def test_gateway_requires_configured_database_port(host_config, port):
    host_config["app_environment"]["DATABASE_URL"] = (
        f"postgresql://dl_gateway:private-password@127.0.0.1{port}/draft"
    )
    with pytest.raises(ValueError, match="port 5432"):
        bootstrap.validate(host_config)


@pytest.mark.parametrize("port", ["", ":5432"])
def test_standard_gateway_port_is_accepted(host_config, port):
    host_config["app_environment"]["DATABASE_URL"] = (
        f"postgresql://dl_gateway:private-password@127.0.0.1{port}/draft"
    )
    assert bootstrap.validate(host_config) is host_config


def test_missing_tls_fails_before_saving_configuration(host_config, tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "host_preflight", lambda: None)
    monkeypatch.setattr(
        bootstrap, "save_configuration", lambda *args: pytest.fail("must not save configuration")
    )
    with pytest.raises(RuntimeError, match="TLS"):
        bootstrap.bootstrap(host_config, tmp_path)


@pytest.fixture
def host_paths(tmp_path, monkeypatch):
    etc = tmp_path / "etc"
    pg = etc / "postgresql" / "16" / "main"
    pg.mkdir(parents=True)
    (pg / "postgresql.conf").write_text("# Ubuntu PostgreSQL 16 cluster")
    monkeypatch.setattr(bootstrap, "ETC", etc)
    monkeypatch.setattr(bootstrap, "PG_DIRECTORY", pg)
    return etc, pg


def test_postgres_installs_only_restricted_remote_certificate_rules(
    host_config, host_paths, monkeypatch
):
    etc, pg = host_paths
    calls, writes = [], []
    atomic_write = bootstrap.atomic_write

    def write(path, content, mode=0o644):
        writes.append((path, mode))
        return atomic_write(path, content, mode)

    monkeypatch.setattr(bootstrap, "atomic_write", write)
    monkeypatch.setattr(bootstrap, "run", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(bootstrap, "check_database_tls", lambda name: None)
    tls = dict.fromkeys(bootstrap.TLS_FILES, b"validated TLS material")
    bootstrap.configure_postgres(host_config, tls)
    hba = (pg / "pg_hba.conf").read_text().splitlines()[1:]
    assert [line.split() for line in hba] == [
        ["local", "all", "postgres", "peer"],
        ["host", "draft", "dl_gateway", "127.0.0.1/32", "scram-sha-256"],
        ["hostssl", "postgres,draft", "postgres", "0.0.0.0/0", "cert"],
        ["hostssl", "draft", "dl_importer", "0.0.0.0/0", "cert"],
        ["host", "all", "all", "0.0.0.0/0", "reject"],
        ["host", "all", "all", "::/0", "reject"],
    ]
    configuration = (pg / "conf.d" / "99-draft.conf").read_text()
    assert "listen_addresses = '0.0.0.0'" in configuration
    assert "port = '5432'" in configuration
    assert "ssl = 'on'" in configuration
    key = etc / "postgresql" / "draft-tls" / "server.key"
    assert (key, 0o640) in writes
    assert ("chown", "root:postgres", key) in calls
    assert ("systemctl", "restart", "postgresql@16-main.service") in calls
    calls.clear()
    bootstrap.configure_postgres(host_config, tls)
    assert ("systemctl", "start", "postgresql@16-main.service") in calls
    assert ("systemctl", "restart", "postgresql@16-main.service") not in calls


def test_interrupted_configuration_retries_postgres_restart(host_config, host_paths, monkeypatch):
    etc, _ = host_paths
    calls = []

    def fail_restart(*args, **kwargs):
        if args[:2] == ("systemctl", "restart"):
            raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(bootstrap, "run", fail_restart)
    monkeypatch.setattr(bootstrap, "check_database_tls", lambda name: None)
    tls = dict.fromkeys(bootstrap.TLS_FILES, b"validated TLS material")
    with pytest.raises(subprocess.CalledProcessError):
        bootstrap.configure_postgres(host_config, tls)
    marker = etc / "draft" / "postgresql-restart-required"
    assert marker.is_file()
    monkeypatch.setattr(bootstrap, "run", lambda *args, **kwargs: calls.append(args))
    bootstrap.configure_postgres(host_config, tls)
    assert ("systemctl", "restart", "postgresql@16-main.service") in calls
    assert not marker.exists()


def test_existing_host_refuses_replaced_ca_before_writing_tls_or_configuration(
    host_config, host_paths, monkeypatch
):
    etc, pg = host_paths
    directory = etc / "postgresql" / "draft-tls"
    directory.mkdir()
    (directory / "ca.crt").write_bytes(b"original deployment CA")
    monkeypatch.setattr(
        bootstrap, "atomic_write", lambda *args: pytest.fail("Must not write configuration")
    )
    monkeypatch.setattr(bootstrap, "run", lambda *args: pytest.fail("Must not change services"))
    tls = dict.fromkeys(bootstrap.TLS_FILES, b"new deployment CA")
    with pytest.raises(RuntimeError, match="uploaded CA differs"):
        bootstrap.configure_postgres(host_config, tls)
    assert (directory / "ca.crt").read_bytes() == b"original deployment CA"
    assert not (directory / "server.key").exists()
    assert not (pg / "pg_hba.conf").exists()


def test_ca_replacement_is_refused_before_saving_configuration(
    host_config, host_paths, monkeypatch
):
    etc, _ = host_paths
    directory = etc / "postgresql" / "draft-tls"
    directory.mkdir()
    (directory / "ca.crt").write_bytes(b"original deployment CA")
    monkeypatch.setattr(bootstrap, "host_preflight", lambda: None)
    monkeypatch.setattr(
        bootstrap, "validate_tls_source", lambda path: {"ca.crt": b"different CA"}
    )
    monkeypatch.setattr(
        bootstrap, "save_configuration", lambda *args: pytest.fail("Must not change app.env")
    )
    with pytest.raises(RuntimeError, match="uploaded CA differs"):
        bootstrap.bootstrap(host_config, etc / "uploaded")


@pytest.fixture
def runtime_check(host_paths, monkeypatch):
    etc, pg = host_paths
    (pg / "pg_hba.conf").write_bytes(bootstrap.PG_HBA)
    monkeypatch.setattr(bootstrap, "validate_tls_source", lambda path: {})
    monkeypatch.setitem(sys.modules, "grp", SimpleNamespace(
        getgrnam=lambda name: SimpleNamespace(gr_gid=44)
    ))
    original_stat = Path.stat

    def path_stat(path, *args, **kwargs):
        if path == etc / "postgresql" / "draft-tls" / "server.key":
            return SimpleNamespace(st_uid=0, st_gid=44, st_mode=0o100640)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", path_stat)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda seconds: None)
    return {
        "settings": bootstrap.postgres_settings("draft"),
        "pending_restart": False,
        "hba_errors": 0,
        "loaded_at": "before",
        "reload": False,
    }


def test_firewall_prerequisite_waits_for_effective_authentication_reload(
    runtime_check, monkeypatch
):
    calls = []
    states = iter([
        runtime_check,
        {**runtime_check, "reload": True},
        runtime_check,
        {**runtime_check, "loaded_at": "after"},
    ])

    def state(*, reload=False):
        calls.append(reload)
        return next(states)

    monkeypatch.setattr(bootstrap, "postgres_state", state)
    bootstrap.check_database_tls("draft")
    assert calls == [False, True, False, False]


@pytest.mark.parametrize("fault", ["wrong_port", "tls_off", "pending_restart", "hba_errors"])
def test_firewall_prerequisite_refuses_unsafe_running_configuration(
    runtime_check, monkeypatch, fault
):
    if fault == "wrong_port":
        runtime_check["settings"]["port"] = "6543"
    elif fault == "tls_off":
        runtime_check["settings"]["ssl"] = "off"
    else:
        runtime_check[fault] = True
    monkeypatch.setattr(bootstrap, "postgres_state", lambda **kwargs: runtime_check)
    with pytest.raises(RuntimeError, match="settings differ|configuration contains errors"):
        bootstrap.check_database_tls("draft")


def test_firewall_prerequisite_refuses_broader_hba(runtime_check, host_paths, monkeypatch):
    _, pg = host_paths
    with (pg / "pg_hba.conf").open("ab") as stream:
        stream.write(b"host all all 0.0.0.0/0 trust\n")
    monkeypatch.setattr(
        bootstrap, "postgres_state", lambda **kwargs: pytest.fail("must reject HBA")
    )
    with pytest.raises(RuntimeError, match="authentication rules differ"):
        bootstrap.check_database_tls("draft")


def test_firewall_prerequisite_requires_completed_reload(runtime_check, monkeypatch):
    monkeypatch.setattr(
        bootstrap, "postgres_state", lambda **kwargs: {**runtime_check, "reload": True}
    )
    ticks = iter([0, 11])
    monkeypatch.setattr(bootstrap.time, "monotonic", lambda: next(ticks))
    with pytest.raises(RuntimeError, match="did not finish reloading"):
        bootstrap.check_database_tls("draft")


@pytest.mark.parametrize("uid,gid,mode", [(100, 44, 0o640), (0, 100, 0o640), (0, 44, 0o644)])
def test_firewall_prerequisite_refuses_unsafe_key_permissions(
    runtime_check, monkeypatch, uid, gid, mode
):
    monkeypatch.setattr(Path, "stat", lambda *args, **kwargs: SimpleNamespace(
        st_uid=uid, st_gid=gid, st_mode=mode
    ))
    with pytest.raises(RuntimeError, match="root:postgres with mode 0640"):
        bootstrap.check_database_tls("draft")


def test_postgres_check_uses_peer_authentication_on_standard_port(monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=json.dumps({"checked": True}))

    monkeypatch.setattr(bootstrap, "run", run)
    assert bootstrap.postgres_state() == {"checked": True}
    assert calls[0][:4] == ("runuser", "-u", "postgres", "--")
    assert "--port=5432" in calls[0]
    assert "--host=/var/run/postgresql" in calls[0]
    assert "--no-password" in calls[0]
