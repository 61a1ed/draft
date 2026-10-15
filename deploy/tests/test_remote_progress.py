"""Local subprocess checks for observable, cancellable deployment commands."""

import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import remote


def _text(output):
    return output.decode() if isinstance(output, bytes) else output


def test_timeout_preserves_partial_output_and_reaps_child(monkeypatch):
    popen = subprocess.Popen
    children = []

    def record_child(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(remote.subprocess, "Popen", record_child)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as error:
        remote.run(
            sys.executable,
            "-u",
            "-c",
            "import sys, time; print('ready'); "
            "print('diagnostic', file=sys.stderr); time.sleep(30)",
            timeout=1,
            capture_output=True,
            text=True,
            progress="Waiting for a local test command",
        )

    assert "ready" in _text(error.value.stdout)
    assert "diagnostic" in _text(error.value.stderr)
    assert time.monotonic() - started < 8
    assert children[0].poll() is not None


def test_captured_stdout_keeps_stderr_visible(capfd):
    result = remote.run(
        sys.executable,
        "-u",
        "-c",
        "import sys; print('/tmp/upload.result'); "
        "print('SSH diagnostic remains visible', file=sys.stderr)",
        stdout=subprocess.PIPE,
        text=True,
        timeout=5,
        progress="Creating the test upload directory",
    )

    captured = capfd.readouterr()
    assert result.stdout == "/tmp/upload.result\n"
    assert "SSH diagnostic remains visible" in captured.err


def test_automated_commands_receive_eof_on_stdin():
    result = remote.run(
        sys.executable,
        "-c",
        "import sys; print(repr(sys.stdin.read()))",
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.stdout.strip() == "''"


def test_command_is_visible_without_printing_environment_credentials(capfd):
    command = [sys.executable, "-c", "print('ok')"]
    result = remote.run(
        *command, capture_output=True, text=True,
        env={**os.environ, "SESSION_SECRET": "never-print-this-value"},
    )
    assert result.stdout == "ok\n"
    printed = capfd.readouterr().err
    assert remote.command_text(command) in printed
    assert "SESSION_SECRET" not in printed
    assert "never-print-this-value" not in printed


def test_failed_command_retains_original_exit_and_diagnostics():
    with pytest.raises(subprocess.CalledProcessError) as error:
        remote.run(
            sys.executable,
            "-c",
            "import sys; print('partial result'); "
            "print('specific failure', file=sys.stderr); sys.exit(23)",
            capture_output=True,
            text=True,
            timeout=5,
        )

    assert error.value.returncode == 23
    assert "partial result" in error.value.stdout
    assert "specific failure" in error.value.stderr


def test_silent_command_reports_progress_without_contaminating_its_output(monkeypatch, capfd):
    monkeypatch.setattr(remote, "PROGRESS_INTERVAL", 0.1)
    monkeypatch.setattr(remote, "POLL_INTERVAL", 0.05)
    result = remote.run(
        sys.executable,
        "-u",
        "-c",
        "import time; time.sleep(0.35); print('command result')",
        stdout=subprocess.PIPE,
        text=True,
        timeout=5,
        progress="Waiting for host initialization",
    )

    captured = capfd.readouterr()
    assert result.stdout == "command result\n"
    assert "Waiting for host initialization" in captured.out
    assert "Still waiting:" in captured.out
    assert "elapsed" in captured.out
    assert "Completed:" in captured.out


def test_interrupt_stops_local_process_before_propagating(monkeypatch):
    events = []

    class InterruptedProcess:
        pid = 424242
        returncode = None
        communication_count = 0

        def communicate(self, *, timeout):
            assert timeout is not None and timeout <= 5
            self.communication_count += 1
            events.append("communicate")
            if self.communication_count == 1:
                raise KeyboardInterrupt
            return "", ""

        def poll(self):
            return self.returncode

        def kill(self):
            events.append("kill")
            self.returncode = -9

    child = InterruptedProcess()

    def stop_tree(*args, **kwargs):
        events.append("stop-tree")
        child.returncode = -9

    monkeypatch.setattr(remote.subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(remote.subprocess, "run", stop_tree)
    monkeypatch.setattr(remote.os, "killpg", stop_tree, raising=False)
    with pytest.raises(KeyboardInterrupt):
        remote.run("simulated-command", timeout=60)

    assert child.returncode is not None
    assert events[0] == "communicate"
    assert "stop-tree" in events or "kill" in events
    assert child.communication_count == 2


def test_interactive_command_retains_terminal_and_can_disable_deadline(monkeypatch):
    options = {}

    def create_process(*args, **kwargs):
        options.update(kwargs)
        return SimpleNamespace(returncode=0, communicate=lambda **kwargs: (None, None))

    monkeypatch.setattr(remote.subprocess, "Popen", create_process)
    result = remote.run("ssh", "example", interactive=True, timeout=None)

    assert result.returncode == 0
    assert "stdin" not in options
    assert "creationflags" not in options
    assert "start_new_session" not in options


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), RuntimeError("installation failed")])
def test_failed_upload_preserves_failure_without_another_remote_command(failure, capsys):
    target = remote.Remote.__new__(remote.Remote)
    target.host = "ubuntu@example"
    path = "/tmp/draft-upload.12345678"
    target.upload_directory = lambda: path
    cleaned = []
    target.cleanup_upload = cleaned.append

    with pytest.raises(type(failure)) as error:
        with target.upload() as upload:
            assert upload == path
            raise failure

    assert error.value is failure
    assert cleaned == []
    assert path in capsys.readouterr().err


def test_cleanup_failure_keeps_successful_operation_and_reports_retained_path(capsys):
    target = remote.Remote.__new__(remote.Remote)
    target.host = "ubuntu@example"
    path = "/tmp/draft-upload.12345678"
    target.upload_directory = lambda: path

    def fail_cleanup(upload):
        assert upload == path
        raise subprocess.TimeoutExpired(["ssh", "example", "rm"], 15)

    target.cleanup_upload = fail_cleanup
    with target.upload() as upload:
        assert upload == path

    warning = capsys.readouterr().err
    assert "Operation completed" in warning
    assert "cleanup failed" in warning
    assert path in warning
