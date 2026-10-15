import io
import json
import sys
import tarfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

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


@pytest.mark.parametrize("action", ["init-db", "import-catalog", "verify-db"])
def test_database_cli_needs_no_ssh_files_or_cloud(config, monkeypatch, action):
    calls = []
    monkeypatch.setattr(manage, "load_config", lambda path: config)
    monkeypatch.setattr(manage, "load_state", lambda cfg: {})
    monkeypatch.setattr(remote, "commit_id", lambda *args: REVISION)
    monkeypatch.setattr(remote, "Remote", lambda *args: pytest.fail("Database must not need SSH"))
    monkeypatch.setitem(sys.modules, "cloud", None)
    monkeypatch.setattr(database, "run_database", lambda *a, **kw: calls.append((a, kw)))
    manage.main([action, "--host", "192.0.2.1"])
    assert calls == [((config, {}, manage.DATABASE_STEPS[action], REVISION), {"host": "192.0.2.1"})]


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
    monkeypatch.setattr(manage, "load_config", lambda path: config)
    monkeypatch.setattr(manage, "load_state", lambda cfg: pytest.fail("No host receipt needed"))
    monkeypatch.setattr(
        database.tls, "ensure_certificates", lambda cfg, **kw: calls.append((cfg, kw)) or "tls",
    )
    manage.main([action])
    assert calls == [(config, {"renew": renew})]
