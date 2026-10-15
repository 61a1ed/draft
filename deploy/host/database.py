"""Explicit PostgreSQL initialization and catalog import for a prepared release.

The host creates the PostgreSQL service. This command creates the dedicated
application database only when both that database and all reserved roles are
absent. It never drops, resets, migrates, or silently adopts a partial database.
Successful bootstrap is recorded in a completion marker; subsequent commands
trust that marker, and application readiness checks gate release activation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

SHA = re.compile(r"[0-9a-f]{40}")
NAME = re.compile(r"[a-z][a-z0-9-]{0,19}")


def run(*args, **kwargs):
    command = [str(arg) for arg in args]
    print("+ " + shlex.join(command), file=sys.stderr, flush=True)
    return subprocess.run(command, check=True, **kwargs)


def environment(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        parts = shlex.split(value, posix=True)
        if len(parts) != 1:
            raise ValueError("Invalid saved application environment")
        result[key] = parts[0]
    return result


def clean_environment(**extra) -> dict[str, str]:
    # In particular, ignore inherited PGHOST/PGSERVICE/PGPASSWORD settings.
    # Ubuntu installs runuser in /usr/sbin, outside the ordinary user PATH.
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        **extra,
    }


def psql(database: str, sql: str) -> str:
    args = [
        "runuser",
        "-u",
        "postgres",
        "--",
        "psql",
        "--no-psqlrc",
        "--no-password",
        "--tuples-only",
        "--no-align",
        "--set",
        "ON_ERROR_STOP=1",
        "--dbname",
        database,
    ]
    args += ["--command", sql]
    return run(*args, env=clean_environment(), stdout=subprocess.PIPE, text=True).stdout.strip()


def cluster_state() -> dict:
    return json.loads(
        psql(
            "postgres",
            """
        SELECT json_build_object(
            'version', current_setting('server_version_num')::integer,
            'database_exists', EXISTS (SELECT FROM pg_database WHERE datname = 'draft'),
            'roles', COALESCE((SELECT json_agg(rolname ORDER BY rolname) FROM pg_roles
                WHERE rolname IN ('dl_owner', 'dl_api', 'dl_auth', 'dl_gateway')), '[]'::json)
        )
    """,
        )
    )


class DatabaseAdmin:
    def __init__(self, name: str, *, settings: Path | None = None, root: Path | None = None):
        if not NAME.fullmatch(name):
            raise ValueError("Invalid application name")
        self.name = name
        self.settings = settings or Path(f"/etc/{name}")
        self.root = root or Path(f"/srv/{name}")
        self.marker = self.settings / "database-initialized.json"

    def password(self) -> str:
        url = urlsplit(environment(self.settings / "app.env")["DATABASE_URL"])
        if (
            url.scheme != "postgresql"
            or url.hostname != "127.0.0.1"
            or url.port not in {None, 5432}
            or url.username != "dl_gateway"
            or url.path != "/draft"
            or not url.password
            or url.query
            or url.fragment
        ):
            raise RuntimeError("Saved database settings do not identify the local draft gateway")
        return unquote(url.password)

    def release(self, revision: str) -> Path:
        if not SHA.fullmatch(revision):
            raise ValueError("Supply a full 40-character Git commit ID")
        release = self.root / "releases" / revision
        if release.is_symlink() or not release.is_dir():
            raise RuntimeError("Prepare this release before running database administration")
        if (release / ".deployment-commit").read_text().strip() != revision:
            raise RuntimeError("Release identity does not match the requested commit")
        backend = release / "draft_backend"
        for path in (
            backend / ".venv/bin/python",
            backend / "scripts/bootstrap.py",
            backend / "scripts/import_catalog.py",
        ):
            if not path.is_file():
                raise RuntimeError(
                    "Prepared release is missing its database administration dependencies"
                )
        return backend

    def run_backend(self, backend: Path, filename: str, *args, password: str | None = None):
        extra = {"ADMIN_DATABASE_URL": "dbname=draft user=postgres"}
        if password is not None:
            extra["DL_GATEWAY_PASSWORD"] = password
        run(
            "runuser",
            "-u",
            "postgres",
            "--",
            backend / ".venv/bin/python",
            "-u",
            backend / "scripts" / filename,
            *args,
            env=clean_environment(**extra),
        )

    def verify_initialized(self):
        if not self.marker.is_file():
            raise RuntimeError("Initialize the database before importing a catalog")
        marker = json.loads(self.marker.read_text())
        if marker.get("name") != self.name or marker.get("database") != "draft":
            raise RuntimeError("Database initialization marker belongs to a different deployment")

    def init(self, revision: str):
        backend = self.release(revision)
        if self.marker.exists():
            self.verify_initialized()
            print(
                "Database is already initialized; existing data and credentials were preserved.",
                flush=True,
            )
            return
        password = self.password()
        state = cluster_state()
        if state["version"] < 160000:
            raise RuntimeError("PostgreSQL 16 or newer is required")
        if state["database_exists"] or state["roles"]:
            raise RuntimeError(
                "Unmarked database or reserved roles already exist. Nothing was reset. "
                "Rebuild this disposable host after an interrupted initialization "
                "before retrying."
            )
        run(
            "runuser",
            "-u",
            "postgres",
            "--",
            "createdb",
            "--no-password",
            "--template",
            "template0",
            "--encoding",
            "UTF8",
            "draft",
            env=clean_environment(),
        )
        self.run_backend(backend, "bootstrap.py", password=password)
        marker = {"name": self.name, "database": "draft", "bootstrap_revision": revision}
        descriptor, temporary = tempfile.mkstemp(prefix=".database-", dir=self.settings)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(marker, stream, indent=2)
                stream.write("\n")
            os.replace(temporary, self.marker)
        finally:
            Path(temporary).unlink(missing_ok=True)
        print("Database initialized. No catalog or test users have been added.", flush=True)

    def import_catalog(self, revision: str, csv: Path, image_root: Path):
        backend = self.release(revision)
        self.verify_initialized()
        csv, image_root = csv.resolve(strict=True), image_root.resolve(strict=True)
        imports = (self.root / "imports").resolve(strict=True)
        if (
            not csv.is_relative_to(imports)
            or not image_root.is_relative_to(imports)
            or not csv.is_file()
            or not image_root.is_dir()
        ):
            raise RuntimeError(
                "Catalog input must be staged under this deployment's imports directory"
            )
        self.run_backend(backend, "import_catalog.py", csv, "--image-root", image_root)

    def import_archive(self, revision: str, archive: Path):
        # Tar members are constrained before extraction; no archive symlink or
        # device may be followed when making the files readable by postgres.
        imports = self.root / "imports"
        imports.mkdir(parents=True, exist_ok=True, mode=0o755)
        with tempfile.TemporaryDirectory(prefix=".import-", dir=imports) as temporary:
            temporary = Path(temporary)
            with tarfile.open(archive, "r:gz") as source:
                for member in source.getmembers():
                    if not (member.isfile() or member.isdir()):
                        raise RuntimeError(
                            "Catalog archive may contain only ordinary files and directories"
                        )
                    relative = Path(member.name)
                    if (
                        not relative.parts
                        or relative.is_absolute()
                        or ".." in relative.parts
                        or "\\" in member.name
                    ):
                        raise RuntimeError("Catalog archive contains an invalid path")
                    if member.name != "catalog.csv" and relative.parts[0] != "images":
                        raise RuntimeError(
                            "Catalog archive must contain catalog.csv and images only"
                        )
                source.extractall(temporary, filter="data")
            temporary.chmod(0o755)
            for path in temporary.rglob("*"):
                if path.is_symlink():
                    raise RuntimeError("Catalog archive contains a symbolic link")
                path.chmod(0o755 if path.is_dir() else 0o644)
            self.import_catalog(revision, temporary / "catalog.csv", temporary / "images")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init")
    initialize.add_argument("revision")
    catalog = commands.add_parser("import")
    catalog.add_argument("revision")
    catalog.add_argument("csv", type=Path)
    catalog.add_argument("image_root", type=Path)
    archive = commands.add_parser("import-archive")
    archive.add_argument("revision")
    archive.add_argument("archive", type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run this command through sudo")
    import fcntl

    name = Path(__file__).resolve().parent.name
    if not NAME.fullmatch(name):
        parser.error("Install database.py under /usr/local/lib/APPLICATION first")
    settings = json.loads(Path(f"/etc/{name}/deploy.json").read_text())
    if settings["name"] != name:
        raise RuntimeError("Installed database script does not match the host configuration")
    with open(f"/run/{name}-deploy.lock", "w") as lock:
        print("Waiting for the host deployment lock...", flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
        admin = DatabaseAdmin(name)
        if args.command == "init":
            admin.init(args.revision)
        elif args.command == "import":
            admin.import_catalog(args.revision, args.csv, args.image_root)
        else:
            admin.import_archive(args.revision, args.archive)


if __name__ == "__main__":
    main()
