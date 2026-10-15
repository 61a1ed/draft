import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("exception", "detail"),
    [
        ("RuntimeError('original failure details')", "RuntimeError: original failure details"),
        ("KeyError('missing resource ID')", "KeyError: 'missing resource ID'"),
        (
            "subprocess.CalledProcessError(17, ['ssh', 'example', 'command'])",
            "CalledProcessError: Command '['ssh', 'example', 'command']' returned non-zero exit "
            "status 17.",
        ),
    ],
)
def test_cli_prints_original_traceback(exception, detail):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"""
import runpy
import subprocess
import sys
import config
import remote

config.load_config = lambda path: None
def fail_check(config):
    raise {exception}
remote.local_check = fail_check
sys.argv = ['manage.py', 'check']
runpy.run_path('manage.py', run_name='__main__')
""",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "Traceback (most recent call last):" in result.stderr
    assert "in fail_check" in result.stderr
    assert detail in result.stderr
