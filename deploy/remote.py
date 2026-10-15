"""Workstation orchestration; commands execute only when an operator invokes them."""

from __future__ import annotations

import csv
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path, PurePosixPath, PureWindowsPath

from config import Config

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parent


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


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
            capture_output=True,
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
        capture_output=True,
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
        capture_output=True,
        text=True,
    ).stdout
    if any(line.startswith("160000 ") for line in tree.splitlines()):
        raise ValueError("Release staging does not support Git submodules")
    return revision


def build_catalog_archive(config, destination):
    """Transfer only referenced PNGs; the existing backend importer validates data."""
    root = config.image_root.resolve(strict=True)
    paths = {}
    with config.catalog_csv.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or "image_path" not in reader.fieldnames:
            raise ValueError("Catalog CSV lacks image_path")
        for row in reader:
            relative = (row.get("image_path") or "").strip()
            pure = PurePosixPath(relative)
            if (
                not relative
                or ":" in relative
                or "\\" in relative
                or PureWindowsPath(relative).anchor
                or pure.is_absolute()
                or ".." in pure.parts
            ):
                raise ValueError("Catalog image paths must be relative files beneath IMAGE_ROOT")
            path = (root / relative).resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError("Catalog image escapes IMAGE_ROOT")
            paths[pure.as_posix()] = path
    if not paths:
        raise ValueError("Catalog is empty")
    with tarfile.open(destination, "w:gz") as archive:
        archive.add(config.catalog_csv, arcname="catalog.csv", recursive=False)
        for relative, path in paths.items():
            archive.add(path, arcname="images/" + relative, recursive=False)


class Remote:
    def __init__(self, config, state):
        self.config = config
        self.state = state
        self.host = f"{state.get('ssh_user', 'ubuntu')}@{state['ip']}"
        key = Path(state["private_key"]).resolve()
        known = (config.state_dir / "known_hosts").resolve()
        if not key.is_file() or not known.is_file():
            raise RuntimeError("Provision the SSH key and verify the host key first")
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
        ]

    def ssh(self, *args, **kwargs):
        return run("ssh", *self.options, self.host, shlex.join(args), **kwargs)

    def copy(self, local, remote):
        return run("scp", *self.options, str(local), f"{self.host}:{remote}")

    def upload_directory(self):
        path = self.ssh(
            "mktemp",
            "-d",
            f"/tmp/{self.config.name}-upload.XXXXXXXX",
            capture_output=True,
            text=True,
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
        self.ssh("rm", "-rf", "--", path)

    def setup(self):
        deadline = time.monotonic() + 300
        while True:
            try:
                self.ssh("true", capture_output=True)
                break
            except subprocess.CalledProcessError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "SSH did not become available; check SSH_CIDRS and retry setup"
                    ) from None
                time.sleep(5)
        self.ssh("sudo", "-n", "cloud-init", "status", "--wait")
        settings = host_settings(self.config)
        with tempfile.TemporaryDirectory(dir=self.config.state_dir, prefix="setup-") as directory:
            payload = Path(directory) / "host.json"
            private_json(payload, settings)
            upload = self.upload_directory()
            try:
                for name in ("bootstrap.py", "database.py", "release.py", "start"):
                    self.copy(HERE / "host" / name, upload + "/" + name)
                self.copy(payload, upload + "/host.json")
                self.ssh("sudo", "-n", "python3", upload + "/bootstrap.py", upload + "/host.json")
            finally:
                self.cleanup_upload(upload)

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
        )

    def release(self, action, revision=None):
        args = [
            "sudo",
            "-n",
            "python3",
            f"/usr/local/lib/{self.config.name}/release.py",
            "--config",
            f"/etc/{self.config.name}/deploy.json",
            action,
        ]
        if revision:
            args.append(revision)
        return self.ssh(*args)

    def prepare(self, revision):
        self.push(revision)
        self.release("prepare", revision)

    def initialize_database(self, revision):
        self.ssh(
            "sudo",
            "-n",
            "python3",
            f"/usr/local/lib/{self.config.name}/database.py",
            "init",
            revision,
        )

    def import_catalog(self, revision):
        with tempfile.TemporaryDirectory(dir=self.config.state_dir, prefix="catalog-") as directory:
            archive = Path(directory) / "catalog.tar.gz"
            build_catalog_archive(self.config, archive)
            upload = self.upload_directory()
            try:
                self.copy(archive, upload + "/catalog.tar.gz")
                # Extraction is an installed, constrained operation; no root tar shell expansion.
                self.ssh(
                    "sudo",
                    "-n",
                    "python3",
                    f"/usr/local/lib/{self.config.name}/database.py",
                    "import-archive",
                    revision,
                    upload + "/catalog.tar.gz",
                )
            finally:
                self.cleanup_upload(upload)

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
        self.ssh("sudo", "-n", "journalctl", "-u", unit, "-n", "100", "--no-pager")


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
    for command in ("git", "ssh", "scp"):
        if not shutil.which(command):
            raise RuntimeError(f"Install {command} and add it to PATH")
    if not config.catalog_csv.is_file() or not config.image_root.is_dir():
        raise ValueError("CATALOG_CSV and IMAGE_ROOT must identify the local catalog inputs")
    print(f"Configuration valid: {config.name} in {config.region}")
    print(f"Site: {config.public_url}")
    print(f"Google callback: {config.public_url}{config.callback_path}")
    print(f"Private files: {config.state_dir}")
    print("No AWS calls were made by this check.")
