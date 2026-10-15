import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from host import bootstrap, database

REVISION = "a" * 40


@pytest.mark.parametrize(
    "script,arguments",
    [
        ("bootstrap.py", ["host.json"]),
        ("database.py", ["init", REVISION]),
        ("release.py", ["--config", "deploy.json", "status"]),
    ],
)
def test_host_commands_show_original_traceback(script, arguments):
    program = """
import os, pathlib, runpy, sys, types
os.geteuid = lambda: 0
sys.modules['fcntl'] = types.SimpleNamespace()
def fail_read(*args, **kwargs):
    raise OSError('configuration could not be read: diagnostic detail')
pathlib.Path.read_text = fail_read
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    path = Path(bootstrap.__file__).with_name(script)
    result = subprocess.run(
        [sys.executable, "-c", program, str(path), *arguments],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Traceback (most recent call last):" in result.stderr
    assert script in result.stderr
    assert "in fail_read" in result.stderr
    assert "OSError: configuration could not be read: diagnostic detail" in result.stderr


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


def test_host_contract_accepts_controller_generated_config(host_config):
    assert bootstrap.validate(host_config) is host_config


@pytest.mark.parametrize("callback", ["/g", "/auth/callback", "/login.v2/~callback"])
def test_host_accepts_backend_callback_paths(host_config, callback):
    host_config["callback_path"] = callback
    host_config["app_environment"]["OIDC_CALLBACK_PATH"] = callback
    assert bootstrap.validate(host_config) is host_config


@pytest.mark.parametrize("callback", ["/api/oidc", "/ready", "/auth", "/a/../g"])
def test_host_rejects_backend_callback_conflicts(host_config, callback):
    host_config["callback_path"] = callback
    host_config["app_environment"]["OIDC_CALLBACK_PATH"] = callback
    with pytest.raises(ValueError, match="non-conflicting"):
        bootstrap.validate(host_config)


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "../root"),
        ("name", "postgres"),
        ("hostname", "example.com\nInjected"),
        ("callback_path", "//attacker.example"),
        ("uv_version", "latest"),
        ("node_version", "26.0.0"),
    ],
)
def test_host_rejects_unsafe_or_unsupported_configuration(host_config, field, value):
    host_config[field] = value
    with pytest.raises(ValueError):
        bootstrap.validate(host_config)


def test_host_refuses_unencrypted_or_external_database(host_config):
    host_config["app_environment"]["DATABASE_URL"] = "postgresql://dl_gateway:p@external/draft"
    with pytest.raises(ValueError, match="DATABASE_URL"):
        bootstrap.validate(host_config)


def test_environment_values_round_trip_without_shell_expansion(tmp_path):
    values = {"SECRET": "spaces $HOME `command` #hash \\path \"quote\" 'apostrophe'", "EMPTY": ""}
    path = tmp_path / "app.env"
    path.write_text(bootstrap.environment_text(values), encoding="utf-8")
    assert bootstrap.read_environment(path) == values
    assert database.environment(path) == values
    with pytest.raises(ValueError):
        bootstrap.environment_text({"SECRET": "one\nTWO=bad"})


def test_repeated_setup_preserves_credentials_and_rejects_replacement(host_config, tmp_path):
    bootstrap.save_configuration(host_config, tmp_path)
    original = (tmp_path / "app.env").read_bytes()
    bootstrap.save_configuration(host_config, tmp_path)
    assert (tmp_path / "app.env").read_bytes() == original
    assert "private-password" not in (tmp_path / "deploy.json").read_text()
    host_config["app_environment"]["SESSION_SECRET"] = "changed" * 8
    with pytest.raises(RuntimeError, match="Saved credentials differ"):
        bootstrap.save_configuration(host_config, tmp_path)
    assert (tmp_path / "app.env").read_bytes() == original


def test_setup_keeps_active_caddy_route(tmp_path):
    path = tmp_path / "caddy.json"
    config = bootstrap.initial_caddy("example.com")
    handle = config["apps"]["http"]["servers"]["public"]["routes"][0]["handle"]
    handle[:] = [{"handler": "reverse_proxy", "upstreams": [{"dial": "unix//active.sock"}]}]
    original = json.dumps(config).encode()
    path.write_bytes(original)
    assert not bootstrap.ensure_caddy_config("example.com", path)
    assert path.read_bytes() == original
    with pytest.raises(RuntimeError, match="another hostname"):
        bootstrap.ensure_caddy_config("other.example.com", path)
    assert path.read_bytes() == original


def test_downloaded_archive_must_match_its_specific_checksum(tmp_path):
    archive = tmp_path / "node.tar.xz"
    archive.write_bytes(b"downloaded-runtime")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    bootstrap.verify_checksum(archive, f"{digest}  node.tar.xz\n")
    with pytest.raises(RuntimeError, match="did not contain"):
        bootstrap.verify_checksum(archive, f"{digest}  other.tar.xz\n")
    with pytest.raises(RuntimeError, match="failed its SHA-256"):
        bootstrap.verify_checksum(archive, f"{'0' * 64}  node.tar.xz\n")


def test_unit_and_launcher_keep_runtime_options_in_application():
    unit = bootstrap.service_unit("draft")
    assert "WorkingDirectory=/srv/draft/releases/%i/draft_backend" in unit
    assert "EnvironmentFile=/etc/draft/app.env" in unit
    assert "RuntimeDirectoryMode=0750" in unit
    assert "Group=draft-web" in unit
    assert "Restart=always" in unit
    start = Path(bootstrap.__file__).with_name("start").read_text()
    assert (
        start == "#!/bin/sh\nexec /usr/local/bin/uv run --locked --no-dev python -m draft_backend\n"
    )


@pytest.fixture
def admin(tmp_path, host_config):
    settings, root = tmp_path / "etc", tmp_path / "srv"
    settings.mkdir()
    settings.joinpath("app.env").write_text(
        bootstrap.environment_text(host_config["app_environment"]), encoding="utf-8"
    )
    release = root / "releases" / REVISION
    for filename in (
        "draft_backend/.venv/bin/python",
        "draft_backend/scripts/bootstrap.py",
        "draft_backend/scripts/import_catalog.py",
    ):
        path = release / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("prepared fixture")
    (release / ".deployment-commit").write_text(REVISION)
    return database.DatabaseAdmin("draft", settings=settings, root=root)


def test_database_initialization_uses_password_only_in_environment(admin, monkeypatch):
    calls = []
    monkeypatch.setattr(
        database,
        "cluster_state",
        lambda: {"version": 160010, "database_exists": False, "roles": []},
    )
    monkeypatch.setattr(
        database,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(stdout="installed"),
    )
    admin.init(REVISION)
    assert json.loads(admin.marker.read_text()) == {
        "name": "draft",
        "database": "draft",
        "bootstrap_revision": REVISION,
    }
    assert "private-password" not in admin.marker.read_text()
    backend_call = calls[1]
    assert "private-password" not in str(backend_call[0])
    assert backend_call[1]["env"]["DL_GATEWAY_PASSWORD"] == "private-password"
    assert backend_call[1]["env"]["ADMIN_DATABASE_URL"] == "dbname=draft user=postgres"
    assert backend_call[0][:4] == ("runuser", "-u", "postgres", "--")


@pytest.mark.parametrize(
    "state",
    [
        {"version": 160010, "database_exists": True, "roles": []},
        {"version": 160010, "database_exists": False, "roles": ["dl_gateway"]},
    ],
)
def test_unmarked_database_is_never_reset_or_adopted(admin, monkeypatch, state):
    monkeypatch.setattr(database, "cluster_state", lambda: state)
    monkeypatch.setattr(database, "run", lambda *a, **kw: pytest.fail("Must not modify PostgreSQL"))
    with pytest.raises(RuntimeError, match="Nothing was reset.*Rebuild"):
        admin.init(REVISION)
    assert not admin.marker.exists()


def test_retry_trusts_completion_marker_and_does_not_reinitialize(admin, monkeypatch):
    admin.marker.write_text(json.dumps({"name": "draft", "database": "draft"}))
    monkeypatch.setattr(
        database,
        "cluster_state",
        lambda: pytest.fail("Completed initialization must not audit PostgreSQL"),
    )
    monkeypatch.setattr(admin, "password", lambda: pytest.fail("Must not need bootstrap password"))
    monkeypatch.setattr(database, "run", lambda *a, **kw: pytest.fail("Must not recreate database"))
    admin.init(REVISION)


@pytest.mark.parametrize(
    "marker", [{"name": "other", "database": "draft"}, {"name": "draft", "database": "other"}]
)
@pytest.mark.parametrize("command", ["init", "import"])
def test_database_commands_reject_another_deployments_marker(admin, monkeypatch, marker, command):
    admin.marker.write_text(json.dumps(marker))
    monkeypatch.setattr(database, "run", lambda *a, **kw: pytest.fail("Must not modify PostgreSQL"))
    with pytest.raises(RuntimeError, match="different deployment"):
        if command == "init":
            admin.init(REVISION)
        else:
            admin.import_catalog(REVISION, admin.root / "catalog.csv", admin.root / "images")


def test_database_failure_does_not_publish_initialization_marker(admin, monkeypatch):
    monkeypatch.setattr(
        database,
        "cluster_state",
        lambda: {"version": 160010, "database_exists": False, "roles": []},
    )
    monkeypatch.setattr(database, "run", lambda *a, **kw: None)
    monkeypatch.setattr(
        admin,
        "run_backend",
        lambda *a, **kw: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["python", "bootstrap.py"])
        ),
    )
    with pytest.raises(subprocess.CalledProcessError):
        admin.init(REVISION)
    assert not admin.marker.exists()


def test_postgres_admin_ignores_inherited_connection_settings(monkeypatch):
    calls = []
    monkeypatch.setenv("PGHOST", "external")
    monkeypatch.setenv("PGSERVICE", "external")
    monkeypatch.setenv("PGPASSWORD", "private")
    monkeypatch.setattr(
        database,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(stdout="ok"),
    )
    assert database.psql("postgres", "SELECT 1") == "ok"
    assert "private" not in str(calls[0][0])
    assert not {"PGHOST", "PGSERVICE", "PGPASSWORD"} & calls[0][1]["env"].keys()
    assert "--no-password" in calls[0][0]


def test_catalog_import_trusts_completion_marker(admin, monkeypatch):
    admin.marker.write_text(json.dumps({"name": "draft", "database": "draft"}))
    imports = admin.root / "imports"
    images = imports / "images"
    images.mkdir(parents=True)
    csv = imports / "catalog.csv"
    csv.write_text("catalog data")
    monkeypatch.setattr(
        database, "cluster_state", lambda: pytest.fail("Catalog import must not audit PostgreSQL")
    )
    monkeypatch.setattr(admin, "password", lambda: pytest.fail("Must not need bootstrap password"))
    calls = []
    monkeypatch.setattr(admin, "run_backend", lambda *args, **kwargs: calls.append((args, kwargs)))
    admin.import_catalog(REVISION, csv, images)
    assert calls == [
        (
            (
                admin.root / "releases" / REVISION / "draft_backend",
                "import_catalog.py",
                csv.resolve(),
                "--image-root",
                images.resolve(),
            ),
            {},
        )
    ]


def test_catalog_import_requires_completion_marker(admin, monkeypatch):
    monkeypatch.setattr(database, "run", lambda *a, **kw: pytest.fail("Must not modify PostgreSQL"))
    with pytest.raises(RuntimeError, match="Initialize the database"):
        admin.import_catalog(REVISION, admin.root / "catalog.csv", admin.root / "images")


def make_archive(path, entries):
    with tarfile.open(path, "w:gz") as archive:
        for name, content, kind in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(content) if kind == tarfile.REGTYPE else 0
            if kind == tarfile.SYMTYPE:
                member.linkname = "/etc/passwd"
            archive.addfile(member, io.BytesIO(content) if kind == tarfile.REGTYPE else None)


def test_catalog_archive_stages_readable_files_and_cleans_up(admin, monkeypatch, tmp_path):
    archive = tmp_path / "catalog.tar.gz"
    make_archive(
        archive,
        [("catalog.csv", b"csv", tarfile.REGTYPE), ("images/one.png", b"png", tarfile.REGTYPE)],
    )
    observed = []

    def inspect(revision, csv, images):
        assert revision == REVISION and csv.read_bytes() == b"csv"
        assert (images / "one.png").read_bytes() == b"png"
        if os.name != "nt":
            assert csv.stat().st_mode & 0o777 == 0o644
            assert csv.parent.stat().st_mode & 0o777 == 0o755
        observed.append(csv.parent)

    monkeypatch.setattr(admin, "import_catalog", inspect)
    admin.import_archive(REVISION, archive)
    assert len(observed) == 1 and not observed[0].exists()


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", tarfile.REGTYPE),
        ("/absolute", tarfile.REGTYPE),
        ("images/link", tarfile.SYMTYPE),
        ("app.env", tarfile.REGTYPE),
    ],
)
def test_catalog_archive_rejects_unsafe_members(admin, monkeypatch, tmp_path, name, kind):
    archive = tmp_path / "invalid.tar.gz"
    make_archive(archive, [(name, b"content", kind)])
    monkeypatch.setattr(admin, "import_catalog", lambda *a: pytest.fail("Must not import"))
    with pytest.raises(RuntimeError):
        admin.import_archive(REVISION, archive)
    assert list((admin.root / "imports").iterdir()) == []
