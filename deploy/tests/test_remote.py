import json
import sys
import tarfile
from types import SimpleNamespace

import pytest

import remote


@pytest.fixture
def config(tmp_path):
    return SimpleNamespace(
        name="draft",
        region="eu-west-1",
        state_dir=tmp_path / "private",
        hostname="example.com",
        callback_path="/g",
        uv_version="0.12.20",
        node_version="24.22.0",
        public_url="https://example.com",
        oidc_client_id="private-id",
        oidc_client_secret="private-secret",
        catalog_csv=tmp_path / "catalog.csv",
        image_root=tmp_path / "images",
    )


def test_secrets_survive_setup_retries_and_reset_after_teardown(config):
    from host.bootstrap import validate

    first = remote.host_settings(config)
    assert validate(first) is first
    second = remote.host_settings(config)
    assert first == second
    saved = json.loads((config.state_dir / "secrets.json").read_text())
    assert saved["session_secret"] in first["app_environment"]["SESSION_SECRET"]
    assert first["app_environment"]["OIDC_CALLBACK_PATH"] == "/g"
    (config.state_dir / "secrets.json").unlink()
    third = remote.host_settings(config)
    assert third["app_environment"]["SESSION_SECRET"] != first["app_environment"]["SESSION_SECRET"]


def test_ssh_checks_host_identity_and_quotes_remote_arguments(config, monkeypatch):
    config.state_dir.mkdir()
    key = config.state_dir / "key with spaces"
    key.write_text("key")
    (config.state_dir / "known_hosts").write_text("verified")
    client = remote.Remote(config, {"ip": "192.0.2.1", "private_key": str(key)})
    calls = []
    monkeypatch.setattr(remote, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    client.ssh("echo", "literal;$(touch /bad)")
    command = calls[0][0]
    assert "StrictHostKeyChecking=yes" in command
    assert "BatchMode=yes" in command
    assert command[-1] == "echo 'literal;$(touch /bad)'"


def test_catalog_only_contains_referenced_files(config, tmp_path):
    config.image_root.mkdir()
    (config.image_root / "one.png").write_bytes(b"image")
    (config.image_root / "unrelated.txt").write_text("private")
    config.catalog_csv.write_text("dex,species,form,points,image_path\n1,One,,1,one.png\n")
    output = tmp_path / "catalog.tar.gz"
    remote.build_catalog_archive(config, output)
    with tarfile.open(output) as archive:
        assert archive.getnames() == ["catalog.csv", "images/one.png"]


@pytest.mark.parametrize("relative", ["../secret.png", "/etc/passwd", "C:\\secret.png"])
def test_catalog_rejects_external_paths(config, tmp_path, relative):
    config.image_root.mkdir()
    config.catalog_csv.write_text(f"image_path\n{relative}\n")
    with pytest.raises(ValueError, match="relative"):
        remote.build_catalog_archive(config, tmp_path / "bad.tar.gz")


def test_dirty_tree_cannot_silently_deploy_stale_commit(monkeypatch):
    monkeypatch.setattr(
        remote, "run", lambda *args, **kwargs: SimpleNamespace(stdout=" M draft_backend/app.py")
    )
    with pytest.raises(RuntimeError, match="Commit"):
        remote.commit_id()


def test_check_does_not_load_cloud(tmp_path, monkeypatch, config):
    import manage

    calls = []
    monkeypatch.setattr(manage, "load_config", lambda path: config)
    monkeypatch.setattr(remote, "local_check", lambda cfg: calls.append(cfg))
    manage.main(["check"])
    assert calls == [config]


def test_bringup_finishes_activation_before_requesting_manual_dns(monkeypatch, config, capsys):
    import manage

    revision = "a" * 40
    state = {"ip": "192.0.2.42"}
    events = []

    class Target:
        def __init__(self, cfg, saved):
            assert cfg is config and saved is state
            self.state = saved

        def setup(self):
            events.append("setup")

        def prepare(self, commit):
            assert commit == revision
            events.append("prepare")

        def initialize_database(self, commit):
            assert commit == revision
            events.append("init-db")

        def import_catalog(self, commit):
            assert commit == revision
            events.append("import-catalog")

        def release(self, action, commit):
            assert (action, commit) == ("activate", revision)
            assert "Set the DNS A record" not in capsys.readouterr().out
            events.append("activate")

    monkeypatch.setattr(manage, "load_config", lambda path: config)
    monkeypatch.setattr(remote, "commit_id", lambda *args: revision)
    monkeypatch.setattr(remote, "local_check", lambda cfg: events.append("check"))
    monkeypatch.setattr(remote, "Remote", Target)
    monkeypatch.setitem(
        sys.modules,
        "cloud",
        SimpleNamespace(
            provision=lambda cfg: events.append("provision"),
            trust_host=lambda cfg: events.append("trust-host"),
            load_state=lambda cfg: state,
        ),
    )
    manage.main(["bringup"])
    assert events == [
        "check",
        "provision",
        "trust-host",
        "setup",
        "prepare",
        "init-db",
        "import-catalog",
        "activate",
    ]
    assert "Set the DNS A record for example.com to 192.0.2.42." in capsys.readouterr().out


def test_destroy_leaves_dns_manual_and_clears_host_secrets(monkeypatch, config, capsys):
    import manage

    remote.host_settings(config)
    (config.state_dir / "known_hosts").write_text("verified")
    calls = []
    monkeypatch.setattr(manage, "load_config", lambda path: config)
    monkeypatch.setitem(
        sys.modules,
        "cloud",
        SimpleNamespace(destroy=lambda cfg, confirm: calls.append((cfg, confirm))),
    )
    manage.main(["destroy", "--confirm", "draft"])
    assert calls == [(config, "draft")]
    assert not (config.state_dir / "secrets.json").exists()
    assert not (config.state_dir / "known_hosts").exists()
    assert (
        "DNS is manual: restore or remove the A record for example.com" in capsys.readouterr().out
    )
