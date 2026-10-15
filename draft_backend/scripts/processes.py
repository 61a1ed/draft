"""Own foreground process trees and retain their output."""

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

if __package__:
    from .bootstrap import PROJECT
else:
    from bootstrap import PROJECT


class Processes:
    """Own process trees and drain both streams independently, including during startup."""

    def __init__(self, logs: Path):
        self.logs = logs
        logs.mkdir(parents=True, exist_ok=True)
        self.children = []
        self.readers = []
        self.lock = threading.Lock()

    def forward(self, stream, path, tag):
        with stream, path.open("a", encoding="utf-8") as log:
            for line in stream:
                log.write(line)
                log.flush()
                with self.lock:
                    sys.stderr.write(f"[{tag}] {line}")
                    sys.stderr.flush()

    def start(self, name, command, *, cwd=PROJECT, env=None):
        child = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        self.children.append(child)
        for label, stream in (("out", child.stdout), ("err", child.stderr)):
            reader = threading.Thread(
                target=self.forward,
                args=(stream, self.logs / f"{name}.{label}.log", f"{name} {label}"),
                daemon=True,
            )
            reader.start()
            self.readers.append(reader)
        return child

    def close(self):
        failures = []
        for child in reversed(self.children):
            try:
                if os.name == "nt":
                    if child.poll() is None:
                        subprocess.run(
                            ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                            timeout=15,
                        )
                else:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                child.wait(timeout=15)
            except (OSError, subprocess.SubprocessError):
                failures.append(child.pid)
        for reader in self.readers:
            reader.join(timeout=5)
        if failures:
            raise RuntimeError(f"Could not stop owned processes: {failures}")

    def run_command(self, name, command, *, env=None, check=True):
        child = self.start(name, command, env=env)
        result = child.wait(timeout=60)
        self.children.remove(child)
        if check and result:
            raise RuntimeError(f"{name} failed; inspect its log")
        return result
