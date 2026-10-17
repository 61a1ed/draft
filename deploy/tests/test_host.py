import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from host import bootstrap


@pytest.mark.parametrize(
    "script,arguments",
    [
        ("bootstrap.py", ["host.json"]),
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
            "MIGRATION_DATABASE_URL": bootstrap.MIGRATION_DATABASE_URL,
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


@pytest.mark.parametrize("name", ["draft", "draft-preprod"])
def test_python_install_does_not_inherit_ssh_working_directory(
    host_config, monkeypatch, tmp_path, name
):
    host_config["name"] = name
    ssh_home = tmp_path / "ubuntu"
    ssh_home.mkdir()
    monkeypatch.chdir(ssh_home)
    for operation in (
        "host_preflight", "save_configuration", "install_packages", "ensure_accounts",
        "install_uv", "install_node", "install_layout", "configure_services", "validate_tls_source",
        "validate_existing_ca",
    ):
        monkeypatch.setattr(bootstrap, operation, lambda *args: None)
    calls = []
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    bootstrap.bootstrap(host_config, Path(bootstrap.__file__).parent)

    assert len(calls) == 1
    app_install = calls[0]
    assert app_install[0][:4] == ["runuser", "-u", name, "--"]
    assert app_install[1]["cwd"] == f"/var/lib/{name}"
    for command, options in calls:
        assert "--no-config" in command
        assert command[-3:] == ["python", "install", "3.12"]
        assert options["check"] is True
    assert Path.cwd() == ssh_home


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "../root"),
        ("name", "postgres"),
        ("hostname", "example.com\nInjected"),
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


@pytest.mark.parametrize("url", ["", "postgresql://postgres@127.0.0.1/draft"])
def test_host_requires_local_migration_connection(host_config, url):
    host_config["app_environment"]["MIGRATION_DATABASE_URL"] = url
    with pytest.raises(ValueError, match="MIGRATION_DATABASE_URL"):
        bootstrap.validate(host_config)


def test_environment_values_round_trip_without_shell_expansion(tmp_path):
    values = {"SECRET": "spaces $HOME `command` #hash \\path \"quote\" 'apostrophe'", "EMPTY": ""}
    path = tmp_path / "app.env"
    path.write_text(bootstrap.environment_text(values), encoding="utf-8")
    assert bootstrap.read_environment(path) == values
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


def test_setup_adds_migration_connection_to_existing_host(host_config, tmp_path):
    migration_url = host_config["app_environment"].pop("MIGRATION_DATABASE_URL")
    bootstrap.save_configuration(host_config, tmp_path)
    host_config["app_environment"]["MIGRATION_DATABASE_URL"] = migration_url
    bootstrap.save_configuration(host_config, tmp_path)
    assert bootstrap.read_environment(tmp_path / "app.env") == host_config["app_environment"]


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
