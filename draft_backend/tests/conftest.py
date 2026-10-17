"""Opt-in integration runs own one fresh PostgreSQL cluster and retain it afterward."""

import asyncio
import socket
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from draft_backend.app import create_app
from draft_backend.config import Settings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import postgres  # noqa: E402
from scripts.bootstrap import PROJECT  # noqa: E402
from scripts.processes import Processes  # noqa: E402


@dataclass(frozen=True)
class TestDatabase:
    root: Path
    gateway: str = field(repr=False)
    admin: str = field(repr=False)


def pytest_addoption(parser):
    parser.addoption("--integration", action="store_true", help="Run with a fresh local PostgreSQL")
    parser.addoption("--pg-bin", help="PostgreSQL binary directory (otherwise PATH)")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--integration"):
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(
                    pytest.mark.skip(reason="Use --integration to create a test database")
                )


def pytest_terminal_summary(terminalreporter, config):
    root = getattr(config, "integration_run", None)
    if root is not None:
        terminalreporter.write_sep("-", f"Database and logs retained: {root}")


@pytest.fixture(scope="session")
def test_database(request):
    if not request.config.getoption("--integration"):
        pytest.skip("Use --integration to create a test database")
    pg_bin = postgres.find_binaries(request.config.getoption("--pg-bin"))
    runs = PROJECT / ".test-runs"
    runs.mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="integration-", dir=runs))
    request.config.integration_run = run
    root = run / "database"
    processes = Processes(run / "logs")
    # Let the OS choose a currently available port. A competing bind fails startup.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    try:
        postgres.init(root, pg_bin, port, processes)
        admin = postgres.read_environment(root / "admin.env")["ADMIN_DATABASE_URL"]
        gateway = postgres.read_environment(root / ".env")["DATABASE_URL"]
        with psycopg.connect(admin) as conn:
            conn.execute((PROJECT / "sql/test_users.sql").read_text(encoding="utf-8"))
            conn.execute((PROJECT / "tests/catalog.sql").read_text(encoding="utf-8"))
        yield TestDatabase(root, gateway, admin)
    finally:
        processes.close()


@pytest.fixture(scope="session")
def database_url(test_database):
    return test_database.gateway


@pytest.fixture
def client(test_database):
    settings = Settings(
        database_url=test_database.gateway,
        migration_database_url=test_database.admin,
        session_secret="x" * 40,
        public_base_url="http://testserver",
        app_env="test",
        test_login_enabled=True,
        cookie_secure=False,
    )
    with TestClient(
        create_app(settings), backend_options={"loop_factory": asyncio.SelectorEventLoop}
    ) as value:
        yield value
