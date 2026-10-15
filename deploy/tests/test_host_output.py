"""Local-only checks for visible host commands and live backend diagnostics."""

import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from host import bootstrap, database


@pytest.mark.parametrize("module", [bootstrap, database])
def test_host_command_log_omits_environment_and_preserves_parsed_stdout(module, capfd):
    command = [
        sys.executable,
        "-c",
        "import sys; print('parsed result'); print('child diagnostic', file=sys.stderr)",
    ]
    result = module.run(
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


@pytest.mark.parametrize("module", [bootstrap, database])
def test_failed_host_command_keeps_exit_and_live_child_traceback(module, capfd, tmp_path):
    script = tmp_path / "failing host step.py"
    script.write_text("raise RuntimeError('original child failure detail')\n")

    with pytest.raises(subprocess.CalledProcessError) as error:
        module.run(sys.executable, script)

    assert error.value.returncode == 1
    assert error.value.cmd == [sys.executable, str(script)]
    assert error.value.stdout is None and error.value.stderr is None
    diagnostic = capfd.readouterr().err
    assert "Traceback (most recent call last):" in diagnostic
    assert "RuntimeError: original child failure detail" in diagnostic
    assert "+ " + shlex.join([sys.executable, str(script)]) in diagnostic


def test_database_backend_output_arrives_before_backend_exits(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    gate = tmp_path / "continue"
    (scripts / "stream.py").write_text(
        "import os, pathlib, sys, time\n"
        "assert os.environ['DL_GATEWAY_PASSWORD'] == os.environ['TEST_SECRET']\n"
        "print('backend started')\n"
        "print('backend diagnostic', file=sys.stderr)\n"
        "while not pathlib.Path(sys.argv[1]).exists():\n"
        "    time.sleep(0.01)\n"
        "print('backend finished')\n"
    )
    # Replace only the Linux runuser/executable prefix so the real host runner
    # and its stream handling are exercised on Windows as well as Linux.
    driver = """
import os, sys
from pathlib import Path
from host import database
original_run = database.run
def local_backend(*args, **kwargs):
    assert args[:4] == ('runuser', '-u', 'postgres', '--')
    kwargs['env'] = {**os.environ, **kwargs['env']}
    return original_run(sys.executable, *args[5:], **kwargs)
database.run = local_backend
database.DatabaseAdmin('draft').run_backend(
    Path(sys.argv[1]), 'stream.py', sys.argv[2], password=os.environ['TEST_SECRET']
)
"""
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", driver, str(tmp_path), str(gate)],
        cwd=Path(database.__file__).resolve().parents[1],
        env={**os.environ, "TEST_SECRET": "backend-secret-not-for-logs"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    started = threading.Event()
    stdout, stderr = [], []

    def read_output(stream, lines):
        for line in stream:
            lines.append(line)
            if line.strip() == "backend started":
                started.set()

    readers = [
        threading.Thread(target=read_output, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=read_output, args=(process.stderr, stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        assert started.wait(10), "Backend stdout was hidden until the command finished"
        assert process.poll() is None
    finally:
        gate.write_text("continue")
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=5)
        process.stdout.close()
        process.stderr.close()

    assert process.returncode == 0, "".join(stderr)
    assert "".join(stdout) == "backend started\nbackend finished\n"
    diagnostic = "".join(stderr)
    assert "backend diagnostic" in diagnostic
    assert " -u " in diagnostic
    assert "backend-secret-not-for-logs" not in diagnostic
    assert "DL_GATEWAY_PASSWORD" not in diagnostic
