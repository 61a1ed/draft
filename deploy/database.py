"""Run a committed backend's database tools locally over PostgreSQL mutual TLS."""

from __future__ import annotations

import json
import os
import re
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path

import remote
import tls


@contextmanager
def selected_backend(config, revision):
    """Use exactly the selected commit and its locked local Python environment."""
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Supply a full 40-character Git commit ID")
    config.state_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="database-", dir=config.state_dir) as directory:
        root = Path(directory).resolve()
        archive = root / "backend.tar"
        remote.run(
            "git", "-C", str(remote.REPOSITORY), "archive", "--format=tar",
            "--output", str(archive), revision, "draft_backend",
            timeout=60, progress=f"Reading backend revision {revision}",
        )
        with tarfile.open(archive) as source:
            for member in source.getmembers():
                if not (member.isfile() or member.isdir()):
                    raise RuntimeError("Database source must contain only ordinary files")
                parts = Path(member.name).parts
                if not parts or parts[0] != "draft_backend" or ".." in parts:
                    raise RuntimeError("Database source contains an unexpected path")
            source.extractall(root, filter="data")
        backend = root / "draft_backend"
        for relative in ("pyproject.toml", "uv.lock", "scripts/deployment_db.py"):
            if not (backend / relative).is_file():
                raise RuntimeError(
                    "Selected revision lacks the local mTLS database workflow; "
                    "commit these changes and select that revision"
                )
        env = {key: value for key, value in os.environ.items() if not key.startswith("UV_")}
        env.pop("VIRTUAL_ENV", None)
        remote.run(
            "uv", "sync", "--locked", "--no-dev", "--project", str(backend),
            env=env, timeout=900, progress="Preparing the selected backend's local environment",
        )
        yield backend


def connection_environment(config, state, action, host=None):
    address = host or state.get("ip")
    if not address or state.get("destroyed"):
        raise RuntimeError("Supply --host ADDRESS or restore the saved database address")
    if not isinstance(address, str) or any(c.isspace() for c in address) or "," in address:
        raise ValueError("Supply one database IP address or hostname")
    role = "dl_importer" if action == "import" else "postgres"
    certificates = tls.validate_client_certificates(config, role)
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("PG")
        and key not in {"ADMIN_DATABASE_URL", "DATABASE_URL", "DL_GATEWAY_PASSWORD", "PYTHONPATH"}
    }
    env.update({
        "PGHOST": address,
        "PGPORT": "5432",
        "PGSSLMODE": "verify-ca",
        "PGSSLROOTCERT": str((certificates / "ca.crt").resolve()),
        "PGSSLCERT": str((certificates / f"{role}.crt").resolve()),
        "PGSSLKEY": str((certificates / f"{role}.key").resolve()),
    })
    if action == "init":
        path = config.state_dir / "secrets.json"
        if not path.is_file():
            raise RuntimeError("Restore secrets.json from host setup before initializing")
        password = json.loads(path.read_text(encoding="utf-8")).get("gateway_password")
        if not isinstance(password, str) or len(password) < 32 or "\0" in password:
            raise RuntimeError("Invalid saved gateway password; restore secrets.json")
        env["DL_GATEWAY_PASSWORD"] = password
    return env


def run_database(config, state, action, revision, *, host=None):
    if action not in {"init", "import", "verify"}:
        raise ValueError("Unknown database action")
    env = connection_environment(config, state, action, host)
    with selected_backend(config, revision) as backend:
        python = backend / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
        args = [action, "--name", config.name, "--revision", revision]
        if action == "import":
            args += ["--csv", str(config.catalog_csv), "--image-root", str(config.image_root)]
        if action == "verify":
            certificate, key = tls.create_untrusted_client(backend.parent / "untrusted")
            args += ["--untrusted-cert", str(certificate), "--untrusted-key", str(key)]
        remote.run(
            str(python), "-u", str(backend / "scripts/deployment_db.py"), *args,
            env=env, cwd=backend, timeout=900,
            progress=f"Database {action} over mutual TLS (verify-ca)",
        )
