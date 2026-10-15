"""Trusted, installed release administrator. Runs on Ubuntu as root.

Source and builds run as the application user. Both the live and boot Caddy
configuration must select the candidate before any previous service is retired.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import urllib.request
from pathlib import Path


def run(*args, **kwargs):
    if kwargs.get("cwd"):
        print(f"In {kwargs['cwd']}:", file=sys.stderr, flush=True)
    print("+ " + shlex.join(str(arg) for arg in args), file=sys.stderr, flush=True)
    return subprocess.run(args, check=True, **kwargs)


def revision_id(value):
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("Supply a full 40-character Git commit ID")
    return value


def atomic_write(path, data, mode=0o644):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".deploy-")
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Releases:
    def __init__(self, settings):
        self.name = settings["name"]
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,19}", self.name):
            raise ValueError("Invalid application name")
        self.hostname = settings["hostname"]
        self.callback = settings["callback_path"]
        self.repo_owner = settings.get("repo_owner", "ubuntu")
        self.root = Path("/srv") / self.name
        self.releases = self.root / "releases"
        self.config = Path("/etc/caddy/caddy.json")
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def unit(self, revision):
        return f"{self.name}@{revision_id(revision)}.service"

    def socket(self, revision):
        return f"/run/{self.name}-{revision_id(revision)}/http.sock"

    def as_app(self, *args, **kwargs):
        return run(
            "runuser", "-u", self.name, "--", "env", f"HOME=/var/lib/{self.name}", *args, **kwargs
        )

    def stage(self, revision):
        revision_id(revision)
        destination = self.releases / revision
        if destination.exists():
            marker = destination / ".deployment-commit"
            if not marker.is_file() or marker.read_text().strip() != revision:
                raise RuntimeError("Existing release has an unexpected identity")
            return destination
        archive = run(
            "runuser",
            "-u",
            self.repo_owner,
            "--",
            "git",
            f"--git-dir={self.root / 'repo.git'}",
            "archive",
            "--format=tar",
            revision,
            "draft_backend",
            "draft_frontend",
            stdout=subprocess.PIPE,
        ).stdout
        temporary = Path(tempfile.mkdtemp(prefix=".stage-", dir=self.releases))
        try:
            with tarfile.open(fileobj=io.BytesIO(archive)) as source:
                source.extractall(temporary, filter="data")
            required = (
                "draft_backend/pyproject.toml",
                "draft_backend/uv.lock",
                "draft_backend/.python-version",
                "draft_frontend/package.json",
                "draft_frontend/package-lock.json",
            )
            if not all((temporary / path).is_file() for path in required):
                raise RuntimeError("Commit lacks required backend/frontend release inputs")
            (temporary / ".deployment-commit").write_text(revision + "\n")
            # Do not dereference source symlinks during privileged ownership changes.
            import pwd

            account = pwd.getpwnam(self.name)
            for parent, directories, files in os.walk(temporary):
                os.chown(parent, account.pw_uid, account.pw_gid)
                for name in directories + files:
                    os.chown(
                        Path(parent) / name, account.pw_uid, account.pw_gid, follow_symlinks=False
                    )
            temporary.chmod(0o755)
            temporary.rename(destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return destination

    def asset_archive(self, directory):
        # Build directories are app-owned. Read their contents as that same user,
        # so even a malicious symlink cannot publish root-only files as assets.
        program = """
from pathlib import Path
import sys, tarfile
dist = Path(sys.argv[1])
source = dist / 'assets'
if (dist.is_symlink() or source.is_symlink() or not source.is_dir()
        or not (dist / 'index.html').is_file()):
    raise SystemExit('Frontend build lacks a regular dist directory and index.html')
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
    for path in sorted(source.rglob('*')):
        if path.is_symlink():
            raise SystemExit('Frontend asset symlinks are unsupported')
        if path.is_file():
            archive.add(path, arcname=path.relative_to(source).as_posix(), recursive=False)
"""
        return self.as_app(
            "/usr/bin/python3",
            "-c",
            program,
            str(directory / "draft_frontend/dist"),
            stdout=subprocess.PIPE,
        ).stdout

    def publish_assets(self, directory):
        """Keep old hashed URLs usable during handoff and rollback."""
        from pathlib import PurePosixPath

        archive_data = self.asset_archive(directory)
        target_root = self.root / "assets"
        target_root.mkdir(exist_ok=True, mode=0o755)
        with tarfile.open(fileobj=io.BytesIO(archive_data)) as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if not member.isfile() or path.is_absolute() or ".." in path.parts:
                    raise RuntimeError("Invalid frontend asset archive entry")
                target = target_root.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                content = archive.extractfile(member).read()
                if target.exists():
                    if target.read_bytes() != content:
                        raise RuntimeError(
                            "Frontend asset name collision; existing bytes preserved"
                        )
                else:
                    atomic_write(target, content)

    def prepare(self, revision):
        directory = self.stage(revision)
        marker = directory / ".prepared"
        if not marker.exists():
            self.as_app(
                "/usr/local/bin/uv", "sync", "--locked", "--no-dev", cwd=directory / "draft_backend"
            )
            self.as_app(
                "/usr/local/bin/npm",
                "ci",
                "--no-audit",
                "--no-fund",
                cwd=directory / "draft_frontend",
            )
            self.as_app("/usr/local/bin/npm", "run", "build", cwd=directory / "draft_frontend")
            self.publish_assets(directory)
            atomic_write(marker, (revision + "\n").encode())
        else:
            # Recover shared assets if an earlier operation was interrupted.
            self.publish_assets(directory)
        return directory

    def route(self, revision):
        backend_paths = [
            "/api",
            "/api/*",
            "/auth",
            "/auth/*",
            "/images",
            "/images/*",
            "/healthz",
            "/ready",
            "/docs",
            "/docs/*",
            "/openapi.json",
            self.callback,
        ]
        return {
            "@id": "app",
            "match": [{"host": [self.hostname]}],
            "handle": [
                {
                    "handler": "subroute",
                    "routes": [
                        {
                            "match": [{"path": list(dict.fromkeys(backend_paths))}],
                            "handle": [
                                {
                                    "handler": "reverse_proxy",
                                    "upstreams": [{"dial": "unix/" + self.socket(revision)}],
                                }
                            ],
                            "terminal": True,
                        },
                        {
                            "match": [{"path": ["/assets/*"]}],
                            "handle": [
                                {"handler": "rewrite", "strip_path_prefix": "/assets"},
                                {
                                    "handler": "headers",
                                    "response": {
                                        "set": {
                                            "Cache-Control": ["public, max-age=31536000, immutable"]
                                        }
                                    },
                                },
                                {"handler": "file_server", "root": str(self.root / "assets")},
                            ],
                            "terminal": True,
                        },
                        {
                            "handle": [
                                {
                                    "handler": "headers",
                                    "response": {"set": {"Cache-Control": ["no-cache"]}},
                                },
                                {
                                    "handler": "file_server",
                                    "root": str(self.releases / revision / "draft_frontend/dist"),
                                },
                            ],
                            "terminal": True,
                        },
                    ],
                }
            ],
            "terminal": True,
        }

    def candidate_config(self, revision):
        config = json.loads(self.config.read_bytes())
        routes = config["apps"]["http"]["servers"]["public"]["routes"]
        matching = [
            i for i, route in enumerate(routes) if route.get("match") == [{"host": [self.hostname]}]
        ]
        if len(matching) != 1:
            raise RuntimeError("Expected exactly one managed hostname route")
        routes[matching[0]] = self.route(revision)
        return (json.dumps(config, indent=2) + "\n").encode()

    def wait_ready(self, revision, timeout=180):
        print(f"Waiting for release {revision} readiness (up to {timeout}s)...", flush=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = subprocess.run(
                [
                    "runuser",
                    "-u",
                    "caddy",
                    "--",
                    "curl",
                    "--silent",
                    "--noproxy",
                    "*",
                    "--max-time",
                    "2",
                    "--unix-socket",
                    self.socket(revision),
                    "--output",
                    "/dev/null",
                    "--write-out",
                    "%{http_code}",
                    f"http://{self.hostname}/ready",
                ],
                capture_output=True,
                text=True,
            )
            if response.returncode == 0 and response.stdout == "200":
                return
            time.sleep(0.5)
        raise RuntimeError(f"Candidate is not ready; inspect journalctl -u {self.unit(revision)}")

    def load_caddy(self, data):
        request = urllib.request.Request(
            "http://127.0.0.1:2019/load",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.http.open(request, timeout=60) as response:
            response.read()

    def retire(self, revision):
        # Include enabled but unloaded instances, which list-units alone misses.
        result = run(
            "systemctl",
            "list-unit-files",
            "--no-legend",
            "--no-pager",
            f"{self.name}@*.service",
            stdout=subprocess.PIPE,
            text=True,
        )
        loaded = run(
            "systemctl",
            "list-units",
            "--all",
            "--plain",
            "--no-legend",
            "--no-pager",
            f"{self.name}@*.service",
            stdout=subprocess.PIPE,
            text=True,
        )
        names = {
            line.split()[0]
            for line in (result.stdout + "\n" + loaded.stdout).splitlines()
            if line.strip()
        }
        for name in sorted(names):
            if re.fullmatch(
                re.escape(self.name) + r"@[0-9a-f]{40}\.service", name
            ) and name != self.unit(revision):
                result = subprocess.run(["systemctl", "disable", "--now", name])
                if result.returncode:
                    print(f"Warning: could not retire {name}", flush=True)

    def activate(self, revision):
        self.prepare(revision)
        data = self.candidate_config(revision)
        run("systemctl", "start", self.unit(revision))
        self.wait_ready(revision)
        run("systemctl", "enable", self.unit(revision))
        # Deliberately retain both processes after ambiguous API/persistence errors.
        print("Switching Caddy to the ready release...", flush=True)
        self.load_caddy(data)
        atomic_write(self.config, data)
        print(f"Active release: {revision}", flush=True)
        try:
            self.retire(revision)
        except (OSError, subprocess.CalledProcessError):
            traceback.print_exc()
            print(
                "Warning: release is active, but old-service cleanup failed; inspect status.",
                flush=True,
            )

    def status(self):
        saved = json.loads(self.config.read_bytes())
        with self.http.open("http://127.0.0.1:2019/config/", timeout=10) as response:
            live = json.load(response)

        def selected(config):
            routes = config["apps"]["http"]["servers"]["public"]["routes"]
            return [route for route in routes if route.get("match") == [{"host": [self.hostname]}]]

        result = {
            "live_matches_saved": selected(live) == selected(saved),
            "live": selected(live),
            "saved": selected(saved),
        }
        print(json.dumps(result, indent=2))
        run("systemctl", "list-units", "--all", "--no-pager", f"{self.name}@*.service")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("action", choices=("prepare", "activate", "status", "stop"))
    parser.add_argument("revision", nargs="?")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run through sudo")
    releases = Releases(json.loads(args.config.read_text()))
    if args.action != "status":
        if not args.revision:
            parser.error("A commit ID is required")
        revision_id(args.revision)
    import fcntl

    with open(f"/run/{releases.name}-deploy.lock", "w") as lock:
        print("Waiting for the host deployment lock...", flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == "status":
            releases.status()
        elif args.action == "stop":
            run("systemctl", "disable", "--now", releases.unit(args.revision))
        else:
            getattr(releases, args.action)(args.revision)


if __name__ == "__main__":
    main()
