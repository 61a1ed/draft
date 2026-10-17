import json
import sys
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
    assert first["app_environment"]["MIGRATION_DATABASE_URL"] == (
        "host=/var/run/postgresql port=5432 dbname=draft user=dl_importer"
    )
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


def test_dirty_tree_cannot_silently_deploy_stale_commit(monkeypatch):
    monkeypatch.setattr(
        remote, "run", lambda *args, **kwargs: SimpleNamespace(stdout=" M draft_backend/app.py")
    )
    with pytest.raises(RuntimeError, match="Commit"):
        remote.commit_id()


def test_check_does_not_load_cloud(tmp_path, monkeypatch, config):
    import manage

    calls = []
    monkeypatch.setattr(manage, "load_config", lambda path, **kwargs: config)
    monkeypatch.setattr(remote, "local_check", lambda cfg: calls.append(cfg))
    manage.main(["check"])
    assert calls == [config]


@pytest.mark.parametrize(
    ("action", "events"),
    [
        ("setup", ["setup"]),
        ("prepare", ["prepare"]),
        ("deploy", ["push", "activate"]),
        ("status", ["status"]),
        ("logs", ["logs"]),
        ("ssh", ["ssh"]),
    ],
)
def test_host_step_runs_independently_without_aws(monkeypatch, config, action, events):
    import manage

    revision = "a" * 40
    state = {"ip": "192.0.2.42"}
    calls = []

    class Target:
        options = []
        host = "ubuntu@192.0.2.42"

        def __init__(self, cfg, saved):
            assert cfg is config and saved is state
            self.state = saved

        def setup(self):
            calls.append("setup")

        def prepare(self, commit):
            assert commit == revision
            calls.append("prepare")

        def push(self, commit):
            assert commit == revision
            calls.append("push")

        def release(self, operation, commit=None):
            assert operation == "status" or commit == revision
            calls.append(operation)

        def logs(self, component, commit):
            assert component == "caddy" and commit is None
            calls.append("logs")

    monkeypatch.setattr(manage, "load_config", lambda path, **kwargs: config)
    monkeypatch.setattr(manage, "load_state", lambda cfg: state)
    monkeypatch.setattr(remote, "commit_id", lambda *args: revision)
    monkeypatch.setattr(remote, "Remote", Target)
    monkeypatch.setattr(remote, "run", lambda *args, **kwargs: calls.append("ssh"))
    monkeypatch.setitem(sys.modules, "cloud", None)  # Even importing AWS code must fail.
    manage.main([action, "caddy"] if action == "logs" else [action])
    assert calls == events


@pytest.mark.parametrize("action", ["delete-instance", "release-ip", "delete-key"])
def test_deletion_is_one_explicit_action_and_preserves_local_secrets(
    monkeypatch, config, capsys, action
):
    import manage

    remote.host_settings(config)
    before = (config.state_dir / "secrets.json").read_bytes()
    (config.state_dir / "known_hosts").write_text("verified")
    calls = []
    monkeypatch.setattr(manage, "load_config", lambda path, **kwargs: config)
    monkeypatch.setitem(
        sys.modules, "cloud",
        SimpleNamespace(**{action.replace("-", "_"): lambda *args: calls.append(args)}),
    )
    manage.main([action, "--confirm", "draft"])
    assert calls == [(config, "draft")]
    assert (config.state_dir / "secrets.json").read_bytes() == before
    assert (config.state_dir / "known_hosts").read_text() == "verified"
    assert "DNS is manual" in capsys.readouterr().out


@pytest.mark.parametrize(
    "args", [["bringup", "--allow-dirty"], ["provision"], ["destroy", "--confirm", "draft"]]
)
def test_retired_commands_stop_before_loading_credentials_or_touching_cloud(monkeypatch, args):
    import manage

    monkeypatch.setattr(
        manage, "load_config", lambda path, **kwargs: pytest.fail("must not load settings"),
    )
    monkeypatch.setitem(sys.modules, "cloud", None)
    with pytest.raises(SystemExit) as error:
        manage.main(args)
    assert error.value.code == 2


def test_steps_is_read_only_and_needs_no_private_settings(monkeypatch, capsys):
    import manage

    monkeypatch.setattr(
        manage, "load_config", lambda path, **kwargs: pytest.fail("must not load settings"),
    )
    monkeypatch.setitem(sys.modules, "cloud", None)
    manage.main(["steps"])
    output = capsys.readouterr().out
    for action in ("create-key", "create-instance", "attach-ip", "setup", "init-db", "deploy"):
        assert action in output
    assert "Run each command separately" in output


def test_existing_bringup_receipt_connects_without_migration_or_aws(config, monkeypatch):
    import manage

    config.state_dir.mkdir()
    key = config.state_dir / "id_rsa"
    key.write_text("existing private key")
    (config.state_dir / "known_hosts").write_text("existing verified host key")
    (config.state_dir / "secrets.json").write_text("existing application secrets")
    receipt = {
        "version": 1, "name": config.name, "region": config.region,
        "hostname": config.hostname, "account_id": "123456789012",
        "deployment_id": "original-deployment", "ip": "192.0.2.42",
        "private_key": str(key), "ssh_user": "ubuntu",
        "key_pair_name": "draft-key-deadbeef", "static_ip_name": "draft-ip-deadbeef",
        "instance_name": "draft", "resources": {},
    }
    (config.state_dir / "state.json").write_text(json.dumps(receipt))
    before = {path.name: path.read_bytes() for path in config.state_dir.iterdir()}
    hosts = []
    monkeypatch.setattr(manage, "load_config", lambda path, **kwargs: config)
    monkeypatch.setattr(remote.Remote, "setup", lambda self: hosts.append(self.host))
    monkeypatch.setitem(sys.modules, "cloud", None)
    manage.main(["setup"])
    assert hosts == ["ubuntu@192.0.2.42"]
    assert {path.name: path.read_bytes() for path in config.state_dir.iterdir()} == before
