import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import remote


@pytest.fixture
def target(tmp_path):
    key = tmp_path / "key"
    key.write_text("test key")
    (tmp_path / "known_hosts").write_text("test host")
    return remote.Remote(
        SimpleNamespace(name="draft", state_dir=tmp_path),
        {"ip": "192.0.2.1", "private_key": str(key)},
    )


def test_connection_is_bounded_and_requires_verified_host_identity(target):
    assert "ConnectTimeout=15" in target.options
    assert "StrictHostKeyChecking=yes" in target.options
    assert "BatchMode=yes" in target.options
    assert "ServerAliveInterval=15" in target.options


def test_certificate_install_uploads_only_server_material_and_skips_setup(target, monkeypatch):
    from contextlib import contextmanager

    import tls

    certificates = target.config.state_dir / "tls"
    monkeypatch.setattr(tls, "validate_server_certificates", lambda config: certificates)
    monkeypatch.setattr(remote, "host_settings", lambda config: pytest.fail("app settings"))
    uploads, commands = [], []
    monkeypatch.setattr(target, "copy", lambda local, dest: uploads.append((local, dest)))
    monkeypatch.setattr(target, "ssh", lambda *args, **kwargs: commands.append(args))

    @contextmanager
    def upload():
        yield "/tmp/draft-upload.abc123"

    monkeypatch.setattr(target, "upload", upload)
    target.install_db_certs()

    assert [path.name for path, dest in uploads] == [
        "bootstrap.py", "server.key", "server.crt", "ca.crt",
    ]
    assert commands == [
        ("mkdir", "-m", "0700", "/tmp/draft-upload.abc123/tls"),
        ("sudo", "-n", "python3", "-u", "/tmp/draft-upload.abc123/bootstrap.py",
         "--install-db-certs", "draft", "/tmp/draft-upload.abc123/tls"),
    ]


def test_mktemp_captures_only_its_result_with_a_deadline(target, monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout="/tmp/draft-upload.abc123\n")

    monkeypatch.setattr(remote, "run", run)
    assert target.upload_directory() == "/tmp/draft-upload.abc123"
    args, kwargs = calls[0]
    assert "ServerAliveInterval=15" in args
    assert "ServerAliveCountMax=3" in args
    assert "-T" in args
    assert kwargs["stdout"] == subprocess.PIPE
    assert "capture_output" not in kwargs and "stderr" not in kwargs
    assert kwargs["timeout"] == 30
    assert "upload directory" in kwargs["progress"]


def test_setup_stops_at_first_ssh_failure_without_retrying(target, monkeypatch):
    attempts = []
    failure = subprocess.CalledProcessError(255, ["ssh"])

    def ssh(*args, **kwargs):
        assert args == ("true",)
        attempts.append(kwargs["timeout"])
        assert "capture_output" not in kwargs and "stderr" not in kwargs
        raise failure

    monkeypatch.setattr(target, "ssh", ssh)
    with pytest.raises(subprocess.CalledProcessError) as error:
        target.setup()
    assert error.value is failure
    assert attempts == [30]


def test_setup_interrupt_stops_before_upload_or_install(target, monkeypatch):
    calls = []

    def ssh(*args, **kwargs):
        calls.append(args)
        if args[0] == "true":
            return SimpleNamespace(stdout="")
        assert args == ("sudo", "-n", "cloud-init", "status", "--wait")
        assert kwargs["timeout"] == 600
        raise KeyboardInterrupt

    monkeypatch.setattr(target, "ssh", ssh)
    monkeypatch.setattr(target, "upload_directory", lambda: pytest.fail("upload after Ctrl-C"))
    with pytest.raises(KeyboardInterrupt):
        target.setup()
    assert len(calls) == 2


def test_cli_ctrl_c_exits_cleanly_without_success_or_traceback():
    result = subprocess.run(
        [sys.executable, "-c", """
import config
import remote
import runpy
import sys
config.load_config = lambda path, **kwargs: None
def cancel(config):
    raise KeyboardInterrupt
remote.local_check = cancel
sys.argv = ['manage.py', 'check']
runpy.run_path('manage.py', run_name='__main__')
"""],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 130
    assert "Cancelled by Ctrl-C. Deployment did not complete." in result.stderr
    assert "Traceback" not in result.stderr
    assert "complete" not in result.stdout
