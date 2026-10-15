"""Local-only checks for visible host commands and live diagnostics."""

import os
import shlex
import subprocess
import sys

import pytest

from host import bootstrap


def test_host_command_log_omits_environment_and_preserves_parsed_stdout(capfd):
    command = [
        sys.executable,
        "-c",
        "import sys; print('parsed result'); print('child diagnostic', file=sys.stderr)",
    ]
    result = bootstrap.run(
        *command,
        env={**os.environ, "DL_GATEWAY_PASSWORD": "environment-secret-not-for-logs"},
        stdout=subprocess.PIPE,
        text=True,
    )

    output = capfd.readouterr()
    assert result.stdout == "parsed result\n"
    assert output.out == ""
    assert "+ " + shlex.join(command) in output.err
    assert "child diagnostic" in output.err
    assert "environment-secret-not-for-logs" not in output.err
    assert "DL_GATEWAY_PASSWORD" not in output.err


def test_failed_host_command_keeps_exit_and_live_child_traceback(capfd, tmp_path):
    script = tmp_path / "failing host step.py"
    script.write_text("raise RuntimeError('original child failure detail')\n")

    with pytest.raises(subprocess.CalledProcessError) as error:
        bootstrap.run(sys.executable, script)

    assert error.value.returncode == 1
    assert error.value.cmd == [sys.executable, str(script)]
    assert error.value.stdout is None and error.value.stderr is None
    diagnostic = capfd.readouterr().err
    assert "Traceback (most recent call last):" in diagnostic
    assert "RuntimeError: original child failure detail" in diagnostic
    assert "+ " + shlex.join([sys.executable, str(script)]) in diagnostic
