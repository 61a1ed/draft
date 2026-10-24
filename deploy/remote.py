"""Workstation orchestration; commands execute only when an operator invokes them."""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from config import Config

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parent


PROGRESS_INTERVAL = 15
POLL_INTERVAL = 0.5


def command_text(args):
    """Render a command for the workstation shell; never include environment values."""
    if os.name == "nt":
        quoted = [
            str(arg) if re.fullmatch(r"[A-Za-z0-9_./:\\=-]+", str(arg))
            else "'" + str(arg).replace("'", "''") + "'"
            for arg in args
        ]
        return ("& " if quoted[0].startswith("'") else "") + " ".join(quoted)
    return shlex.join(str(arg) for arg in args)


def _stop_process(process, *, interactive):
    """Stop local children too: Git can own an SSH process holding output pipes open."""
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        if os.name == "nt" and not interactive:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        elif os.name != "nt" and not interactive:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        try:
            return process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            print("Local command stopped, but an output pipe did not close.", file=sys.stderr)
            return None, None
    finally:
        signal.signal(signal.SIGINT, previous)


def run(*args, timeout=60, progress=None, interactive=False, **kwargs):
    """Poll even on Windows, where an unbounded pipe-reader join delays Ctrl-C."""
    print(command_text(args), file=sys.stderr, flush=True)
    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if not interactive:
        kwargs.setdefault("stdin", subprocess.DEVNULL)
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
    started = time.monotonic()
    next_update = started + PROGRESS_INTERVAL
    if progress:
        print(f"{progress}...", flush=True)
    process = subprocess.Popen(args, **kwargs)
    try:
        while True:
            elapsed = time.monotonic() - started
            wait = (
                POLL_INTERVAL if timeout is None
                else min(POLL_INTERVAL, max(0, timeout - elapsed))
            )
            try:
                stdout, stderr = process.communicate(timeout=wait)
                break
            except subprocess.TimeoutExpired as error:
                now = time.monotonic()
                elapsed = now - started
                if timeout is not None and elapsed >= timeout:
                    if progress:
                        print(
                            f"Timed out: {progress} after {elapsed:.0f}s.",
                            file=sys.stderr, flush=True,
                        )
                    raise subprocess.TimeoutExpired(
                        args, timeout, output=error.output, stderr=error.stderr
                    ) from None
                if progress and now >= next_update:
                    print(f"Still waiting: {progress} ({elapsed:.0f}s elapsed).", flush=True)
                    next_update = now + PROGRESS_INTERVAL
    except BaseException as error:
        # Never launch another remote command while handling a cancelled local one.
        try:
            stdout, stderr = _stop_process(process, interactive=interactive)
            if isinstance(error, subprocess.TimeoutExpired):
                # Windows reader threads publish their buffers only when the pipe closes.
                if stdout is not None:
                    error.output = stdout
                if stderr is not None:
                    error.stderr = stderr
        except (OSError, subprocess.SubprocessError) as cleanup_error:
            print(f"Could not finish local process cleanup: {cleanup_error}", file=sys.stderr)
        raise
    result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    if result.returncode and progress:
        print(f"Failed: {progress} (exit {result.returncode}).", file=sys.stderr, flush=True)
    result.check_returncode()
    if progress:
        print(f"Completed: {progress} ({time.monotonic() - started:.0f}s).", flush=True)
    return result


def private_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def host_settings(config: Config):
    path = config.state_dir / "secrets.json"
    if path.exists():
        credentials = json.loads(path.read_text(encoding="utf-8"))
        if not all(
            isinstance(credentials.get(key), str) and len(credentials[key]) >= 32
            for key in ("gateway_password", "session_secret")
        ):
            raise ValueError("Invalid saved secrets.json; preserve the original for this host")
    else:
        credentials = {
            "gateway_password": secrets.token_urlsafe(36),
            "session_secret": secrets.token_urlsafe(48),
        }
        private_json(path, credentials)
    return {
        "name": config.name,
        "hostname": config.hostname,
        "callback_path": config.callback_path,
        "repo_owner": "ubuntu",
        "uv_version": config.uv_version,
        "node_version": config.node_version,
        "app_environment": {
            "DATABASE_URL": "postgresql://dl_gateway:"
            + credentials["gateway_password"]
            + "@127.0.0.1:5432/draft",
            "MIGRATION_DATABASE_URL": (
                "host=/var/run/postgresql port=5432 dbname=draft user=dl_importer"
            ),
            "SESSION_SECRET": credentials["session_secret"],
            "PUBLIC_BASE_URL": config.public_url,
            "APP_ENV": "production",
            "COOKIE_SECURE": "true",
            "TEST_LOGIN_ENABLED": "false",
            "OIDC_ISSUER": "https://accounts.google.com",
            "OIDC_CLIENT_ID": config.oidc_client_id,
            "OIDC_CLIENT_SECRET": config.oidc_client_secret,
            "OIDC_CALLBACK_PATH": config.callback_path,
        },
    }


def commit_id(revision="HEAD", allow_dirty=False):
    """Never silently deploy the current working copy instead of the chosen commit."""
    if not allow_dirty:
        dirty = run(
            "git",
            "-C",
            str(REPOSITORY),
            "status",
            "--porcelain",
            "--",
            "draft_backend",
            "draft_frontend",
            "deploy",
            stdout=subprocess.PIPE,
            text=True,
        ).stdout
        if dirty.strip():
            raise RuntimeError(
                "Commit the application/deploy changes first, or explicitly use "
                "--allow-dirty to deploy only the selected committed revision"
            )
    revision = run(
        "git",
        "-C",
        str(REPOSITORY),
        "rev-parse",
        "--verify",
        "--end-of-options",
        revision + "^{commit}",
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Deployment requires Git SHA-1 commits")
    tree = run(
        "git",
        "-C",
        str(REPOSITORY),
        "ls-tree",
        "-r",
        revision,
        "--",
        "draft_backend",
        "draft_frontend",
        stdout=subprocess.PIPE,
        text=True,
    ).stdout
    if any(line.startswith("160000 ") for line in tree.splitlines()):
        raise ValueError("Release staging does not support Git submodules")
    return revision


class Remote:
    def __init__(self, config, state):
        self.config = config
        self.state = state
        if not state.get("ip") or state.get("destroyed"):
            raise RuntimeError(
                "No saved host IP. Inspect cloud-status and finish the explicit cloud steps first."
            )
        self.host = f"{state.get('ssh_user', 'ubuntu')}@{state['ip']}"
        key = Path(state["private_key"]).resolve()
        known = (config.state_dir / "known_hosts").resolve()
        if not key.is_file():
            raise RuntimeError("Restore the saved SSH private key before connecting to this host")
        if not known.is_file():
            raise RuntimeError("Run trust-host once to verify and save this host's SSH identity")
        # as_posix also makes the same arguments usable by Git's SSH shell on Windows.
        self.options = [
            "-i",
            key.as_posix(),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "UserKnownHostsFile=" + known.as_posix(),
            "-o",
            "ConnectTimeout=15",
            "-o",
            "ConnectionAttempts=1",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
        ]

    def ssh(self, *args, **kwargs):
        kwargs.setdefault("progress", f"SSH to {self.host}: {args[0]}")
        return run("ssh", "-T", *self.options, self.host, shlex.join(args), **kwargs)

    def copy(self, local, remote):
        return run(
            "scp", *self.options, str(local), f"{self.host}:{remote}",
            timeout=600, progress=f"Uploading {Path(local).name} to {self.host}",
        )

    def upload_directory(self):
        path = self.ssh(
            "mktemp",
            "-d",
            f"/tmp/{self.config.name}-upload.XXXXXXXX",
            stdout=subprocess.PIPE,
            text=True,
            timeout=30,
            progress=f"Creating upload directory on {self.host}",
        ).stdout.strip()
        if not re.fullmatch(
            r"/tmp/" + re.escape(self.config.name) + r"-upload\.[A-Za-z0-9]+", path
        ):
            raise RuntimeError("Unexpected remote temporary directory")
        return path

    def cleanup_upload(self, path):
        if not re.fullmatch(
            r"/tmp/" + re.escape(self.config.name) + r"-upload\.[A-Za-z0-9]+", path
        ):
            raise ValueError("Refusing to clean an unexpected remote path")
        self.ssh(
            "rm", "-rf", "--", path, timeout=15,
            progress=f"Removing temporary upload from {self.host}",
        )

    @contextmanager
    def upload(self):
        path = self.upload_directory()
        try:
            yield path
        except BaseException:
            # A disconnected remote installer may still be using the uploaded files.
            print(
                f"Upload retained on {self.host}: {path}. "
                "Inspect it after checking any interrupted remote operation.",
                file=sys.stderr, flush=True,
            )
            raise
        else:
            try:
                self.cleanup_upload(path)
            except (OSError, subprocess.SubprocessError) as error:
                print(
                    f"Operation completed, but upload cleanup failed for {path}: {error}",
                    file=sys.stderr, flush=True,
                )

    def setup(self):
        self.ssh("true", timeout=30, progress=f"Checking SSH on {self.host}")
        self.ssh(
            "sudo", "-n", "cloud-init", "status", "--wait", timeout=600,
            progress="Waiting for Ubuntu first-boot initialization (cloud-init)",
        )
        from tls import ensure_certificates

        certificates = ensure_certificates(self.config)
        settings = host_settings(self.config)
        with tempfile.TemporaryDirectory(dir=self.config.state_dir, prefix="setup-") as directory:
            payload = Path(directory) / "host.json"
            private_json(payload, settings)
            with self.upload() as upload:
                for name in ("bootstrap.py", "release.py", "start"):
                    self.copy(HERE / "host" / name, upload + "/" + name)
                self.ssh("mkdir", "-m", "0700", upload + "/tls")
                for name in ("server.key", "server.crt", "ca.crt"):
                    self.copy(certificates / name, upload + "/tls/" + name)
                self.copy(payload, upload + "/host.json")
                self.ssh(
                    "sudo", "-n", "python3", "-u", upload + "/bootstrap.py", upload + "/host.json",
                    timeout=1800, progress=f"Installing host services on {self.host}",
                )

    def install_db_certs(self):
        from tls import validate_server_certificates

        certificates = validate_server_certificates(self.config)
        with self.upload() as upload:
            self.copy(HERE / "host/bootstrap.py", upload + "/bootstrap.py")
            self.ssh("mkdir", "-m", "0700", upload + "/tls")
            for name in ("server.key", "server.crt", "ca.crt"):
                self.copy(certificates / name, upload + "/tls/" + name)
            self.ssh(
                "sudo", "-n", "python3", "-u", upload + "/bootstrap.py",
                "--install-db-certs", self.config.name, upload + "/tls",
                timeout=120, progress=f"Installing PostgreSQL certificates on {self.host}",
            )

    def check_database_tls(self):
        self.ssh(
            "sudo", "-n", "python3", "-u",
            f"/usr/local/lib/{self.config.name}/bootstrap.py",
            "--check-database-tls", self.config.name,
            timeout=60, progress=f"Checking PostgreSQL TLS configuration on {self.host}",
        )

    def push(self, revision):
        env = dict(os.environ, GIT_SSH_COMMAND=shlex.join(["ssh", *self.options]))
        url = f"{self.host}:/srv/{self.config.name}/repo.git"
        run(
            "git",
            "-C",
            str(REPOSITORY),
            "push",
            "--force",
            url,
            f"{revision}:refs/heads/deploy",
            env=env,
            timeout=600,
            progress=f"Pushing release {revision} to {self.host}",
        )

    def release(self, action, revision=None):
        args = [
            "sudo",
            "-n",
            "python3",
            "-u",
            f"/usr/local/lib/{self.config.name}/release.py",
            "--config",
            f"/etc/{self.config.name}/deploy.json",
            action,
        ]
        if revision:
            args.append(revision)
        return self.ssh(
            *args, timeout=60 if action == "status" else 1800,
            progress=f"Release {action} on {self.host}" + (f" ({revision})" if revision else ""),
        )

    def prepare(self, revision):
        self.push(revision)
        self.release("prepare", revision)

    def logs(self, component, revision=None):
        if component == "app":
            if not revision:
                raise ValueError("App logs require --revision with a full commit ID")
            if not re.fullmatch(r"[0-9a-f]{40}", revision):
                raise ValueError("App logs require a full 40-character Git commit ID")
            unit = f"{self.config.name}@{revision}.service"
        elif component == "postgres":
            unit = "postgresql@16-main.service"
        else:
            unit = "caddy.service"
        self.ssh(
            "sudo", "-n", "journalctl", "-u", unit, "-n", "100", "--no-pager",
            progress=f"Reading {component} logs on {self.host}",
        )


def smoke(config):
    """Public, read-only smoke checks. Browser sign-in/draft writes remain manual."""
    http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for path in ("/", "/ready"):
        with http.open(config.public_url + path, timeout=15) as response:
            body = response.read()
            if response.status != 200:
                raise RuntimeError(f"Unexpected status for {path}")
            if path == "/ready" and json.loads(body).get("ready") is not True:
                raise RuntimeError("Backend is not ready")
            if path == "/" and b"<html" not in body.lower():
                raise RuntimeError("Frontend did not return HTML")
        print(f"HTTPS {path}: OK")
    print("Complete Google login and the two-browser draft checks in the runbook.")


def local_check(config):
    for command in ("uv", "git", "ssh", "scp"):
        if not shutil.which(command):
            raise RuntimeError(f"Install {command} and add it to PATH")
    if not config.catalog_csv.is_file() or not config.image_root.is_dir():
        raise ValueError("CATALOG_CSV and IMAGE_ROOT must identify the local catalog inputs")
    if config.catalog_details is not None and not config.catalog_details.is_file():
        raise ValueError("CATALOG_DETAILS must identify the local Showdown details JSON")
    print(f"Configuration valid: {config.name} in {config.region}")
    print(f"Site: {config.public_url}")
    print(f"Google callback: {config.public_url}{config.callback_path}")
    print(f"Private files: {config.state_dir}")
    print("No AWS calls were made by this check.")
