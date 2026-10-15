"""Windows console regression checks, isolated from the operator's terminal."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import remote


@pytest.mark.skipif(os.name != "nt", reason="Windows console API")
@pytest.mark.parametrize(
    "scenario", ["child", "success", "failure", "timeout", "interrupt", "cleanup", "progress"]
)
def test_console_modes_survive_child_commands(scenario):
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    result = subprocess.run(
        [
            sys.executable, "-c", "import runpy, sys; "
            "runpy.run_path(sys.argv[1], run_name='__main__')",
            str(Path(__file__).resolve()), scenario,
        ],
        cwd=Path(remote.__file__).parent,
        startupinfo=startup,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_non_windows_console_preservation_is_a_noop():
    with patch.object(remote, "os") as platform:
        platform.name = "posix"
        remote._console_mode_restore()()


def _console_scenario(scenario):
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
    kernel32.SetStdHandle.restype = wintypes.BOOL
    kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetConsoleMode.restype = wintypes.BOOL
    kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetConsoleMode.restype = wintypes.BOOL
    kernel32.CreateConsoleScreenBuffer.argtypes = [
        wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p
    ]
    kernel32.CreateConsoleScreenBuffer.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    # stdout/stderr start as redirected pipes. They must be ignored safely.
    remote._console_mode_restore()()
    buffer = kernel32.CreateConsoleScreenBuffer(0xC0000000, 3, None, 1, None)
    assert buffer not in (None, ctypes.c_void_p(-1).value), ctypes.get_last_error()
    original_stdout = kernel32.GetStdHandle(-11)

    def read_mode():
        value = wintypes.DWORD()
        assert kernel32.GetConsoleMode(buffer, ctypes.byref(value))
        return value.value

    def corrupt_mode():
        assert kernel32.SetConsoleMode(buffer, 0)
        assert read_mode() == 0

    try:
        assert kernel32.SetConsoleMode(buffer, 3)
        assert kernel32.SetStdHandle(-11, buffer)
        if scenario == "child":
            # Actual child resets the inherited console mode, as OpenSSH 8.1 does.
            remote.run(
                sys.executable, "-c",
                "import ctypes; from ctypes import wintypes; "
                "k=ctypes.WinDLL('kernel32'); "
                "k.GetStdHandle.argtypes=[wintypes.DWORD]; "
                "k.GetStdHandle.restype=wintypes.HANDLE; "
                "k.SetConsoleMode.argtypes=[wintypes.HANDLE,wintypes.DWORD]; "
                "assert k.SetConsoleMode(k.GetStdHandle(-11),0)",
                timeout=5,
            )
        else:
            class Process:
                returncode = 23 if scenario == "failure" else 0
                calls = 0

                def communicate(self, *, timeout):
                    self.calls += 1
                    corrupt_mode()
                    if scenario in ("interrupt", "cleanup"):
                        raise KeyboardInterrupt
                    if scenario == "timeout" or (scenario == "progress" and self.calls == 1):
                        raise subprocess.TimeoutExpired("test-command", timeout)
                    return None, None

            def stop_process(*args, **kwargs):
                corrupt_mode()
                if scenario == "cleanup":
                    raise OSError("simulated cleanup failure")
                return None, None

            def checked_print(*args, **kwargs):
                assert read_mode() == 3, args

            expected = {
                "failure": subprocess.CalledProcessError,
                "timeout": subprocess.TimeoutExpired,
                "interrupt": KeyboardInterrupt,
                "cleanup": KeyboardInterrupt,
            }.get(scenario)
            with (
                patch.object(remote.subprocess, "Popen", return_value=Process()),
                patch.object(remote, "_stop_process", side_effect=stop_process),
                patch.object(remote, "print", checked_print, create=True),
                patch.object(remote, "PROGRESS_INTERVAL", 0),
            ):
                try:
                    remote.run(
                        "test-command", timeout=0 if scenario == "timeout" else 5,
                        progress="Checking console restoration",
                    )
                except BaseException as error:
                    assert expected is not None and isinstance(error, expected), repr(error)
                else:
                    assert expected is None
        assert read_mode() == 3
    finally:
        assert kernel32.SetStdHandle(-11, original_stdout)
        kernel32.CloseHandle(buffer)


if __name__ == "__main__":
    _console_scenario(sys.argv[2])
