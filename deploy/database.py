"""Verify PostgreSQL mutual TLS and run a committed backend's database tools locally."""

from __future__ import annotations

import json
import os
import re
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path

import psycopg

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
    if (not isinstance(address, str) or any(c.isspace() or c in "/\\,\0" for c in address)
            or address.startswith("@")):
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
    if action not in {"init", "import"}:
        raise ValueError("Unknown database action")
    env = connection_environment(config, state, action, host)
    with selected_backend(config, revision) as backend:
        python = backend / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
        args = [action, "--name", config.name, "--revision", revision]
        if action == "import":
            args += ["--csv", str(config.catalog_csv), "--image-root", str(config.image_root)]
        remote.run(
            str(python), "-u", str(backend / "scripts/deployment_db.py"), *args,
            env=env, cwd=backend, timeout=900,
            progress=f"Database {action} over mutual TLS (verify-ca)",
        )


def rejected_certificate(options: dict[str, str], *, missing: bool) -> None:
    try:
        with psycopg.connect(**options, dbname="postgres", user="postgres"):
            pass
    except psycopg.OperationalError as exc:
        # Only actual server authentication/TLS alerts are evidence. A timeout,
        # broken local PEM, missing file, or server-CA failure cannot pass a test.
        detail = str(exc).lower()
        expected = (
            ("connection requires a valid client certificate",)
            if missing else (
                "alert unknown ca", "alert bad certificate", "alert certificate unknown",
                "alert certificate required", "certificate authentication failed",
                "connection requires a valid client certificate",
            )
        )
        if any(message in detail for message in expected):
            return
        raise RuntimeError("Client-certificate rejection could not be verified") from None
    raise RuntimeError("Server accepted a missing or untrusted client certificate")


def verify_transport(options: dict[str, str], untrusted_cert: Path, untrusted_key: Path) -> None:
    """Check trusted access around missing/untrusted-client authentication failures."""
    if not untrusted_cert.is_file() or not untrusted_key.is_file():
        raise ValueError("Untrusted client certificate and key must be existing files")

    def trusted() -> None:
        # Transport verification also runs on a fresh host before init-db.
        with psycopg.connect(**options, dbname="postgres", user="postgres") as conn:
            if conn.info.server_version < 160000:
                raise RuntimeError("PostgreSQL 16 or newer is required")
            row = conn.execute(
                "SELECT current_user, ssl, client_dn FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
            ).fetchone()
            if not row or row[0] != "postgres" or not row[1] or not row[2]:
                raise RuntimeError("Expected an encrypted connection with the postgres identity")

    # libpq reads its ambient settings even when explicit options are supplied.
    # Restore the caller's environment after this local, synchronous operation.
    ambient = {key: value for key, value in os.environ.items() if key.upper().startswith("PG")}
    for key in ambient:
        del os.environ[key]
    try:
        trusted()
        rejected_certificate({**options, "sslcertmode": "disable"}, missing=True)
        rejected_certificate(
            {**options, "sslcert": str(untrusted_cert.resolve()),
             "sslkey": str(untrusted_key.resolve())}, missing=False,
        )
        trusted()
    except psycopg.Error as error:
        raise RuntimeError(
            f"Database verification failed (SQLSTATE {error.sqlstate or 'connection error'})"
        ) from None
    finally:
        os.environ.update(ambient)


def verify_database(config, state, *, host=None):
    """Verify transport using local certificates, without a backend checkout or SSH."""
    environment = connection_environment(config, state, "verify", host)
    options = {
        "host": environment["PGHOST"],
        "port": environment["PGPORT"],
        "sslmode": "verify-ca",
        "sslrootcert": environment["PGSSLROOTCERT"],
        "sslcert": environment["PGSSLCERT"],
        "sslkey": environment["PGSSLKEY"],
        "sslcertmode": "require",
        "gssencmode": "disable",
        "password": "",
        "passfile": os.devnull,
        "connect_timeout": "10",
        "application_name": "draft-deployment",
        "options": "-c search_path=pg_catalog -c statement_timeout=60000",
    }
    with tempfile.TemporaryDirectory(prefix="verify-db-", dir=config.state_dir) as temporary:
        certificate, key = tls.create_untrusted_client(Path(temporary) / "untrusted")
        verify_transport(options, certificate, key)
    print("Verified trusted mTLS access and rejection of missing/untrusted certificates.")
