import io
import json
import os
import sys
import tarfile
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

import database
import manage
import remote

REVISION = "a" * 40


@pytest.fixture
def config(tmp_path):
    return SimpleNamespace(
        name="draft", state_dir=tmp_path, catalog_csv=tmp_path / "catalog.csv",
        image_root=tmp_path / "images",
    )


@pytest.mark.parametrize("action,role", [("init", "postgres"), ("import", "dl_importer")])
def test_connection_is_explicit_and_ignores_ambient_credentials(config, monkeypatch, action, role):
    (config.state_dir / "secrets.json").write_text(json.dumps({"gateway_password": "s" * 40}))
    for key in ("PGPASSWORD", "PGSERVICE", "PGHOSTADDR", "PGSSLMODE", "DL_GATEWAY_PASSWORD"):
        monkeypatch.setenv(key, "untrusted-default")
    checked = []
    monkeypatch.setattr(
        database.tls, "validate_client_certificates",
        lambda cfg, user: checked.append(user) or cfg.state_dir / "tls",
    )
    env = database.connection_environment(config, {}, action, "192.0.2.1")
    assert checked == [role]
    assert env["PGHOST"] == "192.0.2.1" and env["PGPORT"] == "5432"
    assert env["PGSSLMODE"] == "verify-ca"
    assert env["PGSSLCERT"].endswith(f"{role}.crt")
    assert "PGPASSWORD" not in env and "PGSERVICE" not in env and "PGHOSTADDR" not in env
    assert env.get("DL_GATEWAY_PASSWORD") == ("s" * 40 if action == "init" else None)


@pytest.mark.parametrize("action", ["init-db", "import-catalog"])
def test_database_cli_needs_no_ssh_files_or_cloud(config, monkeypatch, action):
    calls = []
    monkeypatch.setattr(manage, "load_config", lambda path, **kwargs: config)
    monkeypatch.setattr(manage, "load_state", lambda cfg: {})
    monkeypatch.setattr(remote, "commit_id", lambda *args: REVISION)
    monkeypatch.setattr(remote, "Remote", lambda *args: pytest.fail("Database must not need SSH"))
    monkeypatch.setitem(sys.modules, "cloud", None)
    monkeypatch.setattr(database, "run_database", lambda *a, **kw: calls.append((a, kw)))
    manage.main([action, "--host", "192.0.2.1"])
    assert calls == [((config, {}, manage.DATABASE_STEPS[action], REVISION), {"host": "192.0.2.1"})]


def test_verify_cli_needs_no_revision_checkout_cloud_or_receipt(config, monkeypatch):
    calls = []
    monkeypatch.setattr(manage, "load_config", lambda path, **kwargs: config)
    monkeypatch.setattr(manage, "load_state", lambda cfg: pytest.fail("No receipt needed"))
    monkeypatch.setattr(remote, "commit_id", lambda *a: pytest.fail("No revision needed"))
    monkeypatch.setattr(remote, "Remote", lambda *a: pytest.fail("No SSH needed"))
    monkeypatch.setitem(sys.modules, "cloud", None)
    monkeypatch.setattr(database, "selected_backend", lambda *a: pytest.fail("No checkout needed"))
    monkeypatch.setattr(database, "verify_database", lambda *a, **kw: calls.append((a, kw)))
    manage.main(["verify-db", "--host", "192.0.2.1"])
    assert calls == [((config, {}), {"host": "192.0.2.1"})]


@pytest.mark.parametrize("host", ["/tmp/socket", "@socket", "host1,host2", "host\n", "host\0"])
def test_database_target_is_one_tcp_host(config, host):
    with pytest.raises(ValueError, match="one database IP address or hostname"):
        database.connection_environment(config, {}, "verify", host)


def trusted_connection():
    return nullcontext(SimpleNamespace(
        info=SimpleNamespace(server_version=160000),
        execute=lambda query: SimpleNamespace(fetchone=lambda: ("postgres", True, "CN=postgres")),
    ))


def test_verify_uses_local_certificates_and_restores_ambient_environment(config, monkeypatch):
    monkeypatch.setenv("PGSERVICE", "ambient-service")
    monkeypatch.setenv("PGHOSTADDR", "192.0.2.222")
    monkeypatch.setenv("PGPASSWORD", "ambient-secret")
    monkeypatch.setattr(
        database.tls, "validate_client_certificates", lambda cfg, role: cfg.state_dir / "tls",
    )

    def untrusted(directory):
        directory.mkdir()
        certificate, key = directory / "untrusted.crt", directory / "untrusted.key"
        certificate.touch()
        key.touch()
        return certificate, key

    calls = []

    def connect(**kwargs):
        assert not any(key.upper().startswith("PG") for key in os.environ)
        calls.append(kwargs)
        if kwargs["sslcertmode"] == "disable":
            raise psycopg.OperationalError("connection requires a valid client certificate")
        if Path(kwargs["sslcert"]).name == "untrusted.crt":
            raise psycopg.OperationalError("tlsv1 alert unknown ca")
        return trusted_connection()

    monkeypatch.setattr(database.tls, "create_untrusted_client", untrusted)
    monkeypatch.setattr(database.psycopg, "connect", connect)
    monkeypatch.setattr(database, "selected_backend", lambda *a: pytest.fail("No checkout needed"))
    database.verify_database(config, {}, host="192.0.2.1")
    assert len(calls) == 4
    assert calls[0]["host"] == "192.0.2.1"
    assert calls[0]["sslmode"] == "verify-ca" and calls[0]["sslcertmode"] == "require"
    assert calls[0]["password"] == "" and calls[0]["passfile"] == os.devnull
    assert calls[0]["gssencmode"] == "disable"
    assert calls[0]["sslcert"] == str((config.state_dir / "tls/postgres.crt").resolve())
    assert calls[0]["sslkey"] == str((config.state_dir / "tls/postgres.key").resolve())
    assert calls[0]["sslrootcert"] == str((config.state_dir / "tls/ca.crt").resolve())
    assert os.environ["PGSERVICE"] == "ambient-service"
    assert os.environ["PGHOSTADDR"] == "192.0.2.222"
    assert os.environ["PGPASSWORD"] == "ambient-secret"
    assert not list(config.state_dir.glob("verify-db-*"))


def test_verification_sanitizes_connection_failure_and_restores_environment(tmp_path, monkeypatch):
    certificate = tmp_path / "untrusted.crt"
    key = tmp_path / "untrusted.key"
    certificate.touch()
    key.touch()
    monkeypatch.setenv("PGPASSWORD", "ambient-secret")

    def reject(**kwargs):
        raise psycopg.OperationalError("failure with password=ambient-secret")

    monkeypatch.setattr(database.psycopg, "connect", reject)
    with pytest.raises(RuntimeError, match="Database verification failed") as error:
        database.verify_transport({}, certificate, key)
    assert "secret" not in str(error.value)
    assert os.environ["PGPASSWORD"] == "ambient-secret"


def test_import_runs_local_files_in_selected_revision_environment(config, monkeypatch):
    backend = config.state_dir / "draft_backend"
    backend.mkdir()

    @contextmanager
    def selected(cfg, revision):
        assert cfg is config and revision == REVISION
        yield backend

    calls = []
    monkeypatch.setattr(database, "selected_backend", selected)
    monkeypatch.setattr(database, "connection_environment", lambda *a: {"PGSSLMODE": "verify-ca"})
    monkeypatch.setattr(remote, "run", lambda *a, **kw: calls.append((a, kw)))
    database.run_database(config, {}, "import", REVISION)
    args, options = calls[0]
    assert args[2] == str(backend / "scripts/deployment_db.py")
    assert args[3:] == (
        "import", "--name", "draft", "--revision", REVISION,
        "--csv", str(config.catalog_csv), "--image-root", str(config.image_root),
    )
    assert options["env"] == {"PGSSLMODE": "verify-ca"}
    assert options["cwd"] == backend
    assert len(calls) == 1


def test_selected_backend_uses_committed_archive_and_locked_dependencies(config, monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "git":
            path = Path(args[args.index("--output") + 1])
            with tarfile.open(path, "w") as archive:
                for name in ("pyproject.toml", "uv.lock", "scripts/deployment_db.py"):
                    member = tarfile.TarInfo("draft_backend/" + name)
                    member.size = len(b"committed")
                    archive.addfile(member, io.BytesIO(b"committed"))

    monkeypatch.setattr(remote, "run", run)
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "unrelated-env")
    with database.selected_backend(config, REVISION) as backend:
        assert (backend / "scripts/deployment_db.py").read_text() == "committed"
        assert calls[0][0][-2:] == (REVISION, "draft_backend")
        assert calls[1][0][:4] == ("uv", "sync", "--locked", "--no-dev")
        assert "UV_PROJECT_ENVIRONMENT" not in calls[1][1]["env"]
    assert not backend.exists()


def test_setup_uploads_only_server_tls_material(config, monkeypatch):
    key = config.state_dir / "id_rsa"
    key.write_text("ssh-key")
    (config.state_dir / "known_hosts").write_text("verified-host")
    target = remote.Remote(config, {"ip": "192.0.2.1", "private_key": str(key)})
    copied = []
    monkeypatch.setattr(target, "ssh", lambda *a, **kw: None)
    monkeypatch.setattr(target, "upload_directory", lambda: "/tmp/draft-upload.abc")
    monkeypatch.setattr(target, "copy", lambda source, dest: copied.append((source.name, dest)))
    monkeypatch.setattr(remote, "host_settings", lambda cfg: {})
    monkeypatch.setattr(database.tls, "ensure_certificates", lambda cfg: cfg.state_dir / "tls")
    target.setup()
    assert {name for name, _ in copied} == {
        "bootstrap.py", "release.py", "start", "host.json", "server.key", "server.crt", "ca.crt",
    }


@pytest.mark.parametrize("action,renew", [("create-certs", False), ("renew-certs", True)])
def test_certificates_need_no_saved_ip_or_remote_connection(config, monkeypatch, action, renew):
    calls = []
    monkeypatch.setattr(manage, "load_config", lambda path, **kwargs: config)
    monkeypatch.setattr(manage, "load_state", lambda cfg: pytest.fail("No host receipt needed"))
    monkeypatch.setattr(
        database.tls, "ensure_certificates", lambda cfg, **kw: calls.append((cfg, kw)) or "tls",
    )
    manage.main([action])
    assert calls == [(config, {"renew": renew})]


@pytest.mark.parametrize("missing,message", [
    (True, "connection requires a valid client certificate"),
    (False, "SSL error: tlsv1 alert unknown ca"),
    (False, "certificate authentication failed for user postgres"),
])
def test_certificate_negative_checks_require_server_rejection(monkeypatch, missing, message):
    def reject(**kwargs):
        raise psycopg.OperationalError(message)

    monkeypatch.setattr(database.psycopg, "connect", reject)
    database.rejected_certificate({}, missing=missing)


@pytest.mark.parametrize("message", [
    "connection timed out", "could not read private key file", "certificate verify failed",
    "connection refused", "invalid connection option sslcertmode",
])
def test_local_tls_errors_and_outages_do_not_pass_negative_tests(monkeypatch, message):
    def reject(**kwargs):
        raise psycopg.OperationalError(message)

    monkeypatch.setattr(database.psycopg, "connect", reject)
    with pytest.raises(RuntimeError, match="could not be verified"):
        database.rejected_certificate({}, missing=False)


def test_missing_certificate_must_not_be_accepted(monkeypatch):
    monkeypatch.setattr(database.psycopg, "connect", lambda **kwargs: nullcontext())
    with pytest.raises(RuntimeError, match="Server accepted"):
        database.rejected_certificate({}, missing=True)


def test_verification_disables_client_cert_and_rechecks_trusted_after_failures(
    tmp_path, monkeypatch
):
    certificate = tmp_path / "untrusted.crt"
    key = tmp_path / "untrusted.key"
    certificate.touch()
    key.touch()
    options = {"sslmode": "verify-ca", "sslcert": "trusted.crt"}
    calls = []

    def connect(**kwargs):
        calls.append(kwargs)
        if kwargs.get("sslcertmode") == "disable":
            raise psycopg.OperationalError("connection requires a valid client certificate")
        if kwargs["sslcert"] != options["sslcert"]:
            raise psycopg.OperationalError("tlsv1 alert unknown ca")
        return trusted_connection()

    monkeypatch.setattr(database.psycopg, "connect", connect)
    database.verify_transport(options, certificate, key)
    assert len(calls) == 4
    assert calls[0] == calls[-1]
    assert calls[1]["sslcertmode"] == "disable"
    assert all(call["sslmode"] == "verify-ca" for call in calls)
    assert all(call["dbname"] == "postgres" for call in calls)
