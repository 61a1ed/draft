"""Launcher modes, isolation, output forwarding, and owned-process cleanup."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import dev


def test_environments_remove_secrets_and_disable_color(monkeypatch):
    for key in (
        "DATABASE_URL",
        "ADMIN_DATABASE_URL",
        "SESSION_SECRET",
        "MY_DB",
        "OIDC_CLIENT_SECRET",
        "FORCE_COLOR",
    ):
        monkeypatch.setenv(key, "secret")
    backend, frontend = dev.child_environments(
        {"DATABASE_URL": "local", "TEST_LOGIN_ENABLED": "true"}, 8001, 5174, "MY_DB"
    )
    assert backend["DATABASE_URL"] == "local"
    assert backend["PUBLIC_BASE_URL"] == "http://127.0.0.1:5174"
    assert frontend["VITE_ENABLE_TEST_LOGIN"] == "true"
    assert (
        not {
            "DATABASE_URL",
            "ADMIN_DATABASE_URL",
            "SESSION_SECRET",
            "MY_DB",
            "OIDC_CLIENT_SECRET",
            "FORCE_COLOR",
        }
        & frontend.keys()
    )
    assert frontend["NO_COLOR"] == "1"


@pytest.mark.parametrize(
    "args",
    [
        ["--data-dir", "x", "--database-url-env", "DB"],
        ["--database-url-env", "DB", "--db-port", "55440"],
        ["--database-url-env", "DB", "--pg-bin", "bin"],
        ["--api-port", "5173"],
        ["--web-port", "0"],
        ["--prepare-only"],
        ["--root", "x"],
    ],
)
def test_invalid_cli(args):
    with pytest.raises(SystemExit):
        dev.parse_args(args)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "PROJECT", tmp_path)
    monkeypatch.setattr(dev, "FRONTEND", tmp_path)
    vite = tmp_path / "node_modules/vite/bin/vite.js"
    vite.parent.mkdir(parents=True)
    vite.touch()
    monkeypatch.setattr(dev.shutil, "which", lambda name: str(tmp_path / name))
    return tmp_path


def test_network_mode_never_prepares_database(workspace, monkeypatch):
    monkeypatch.setenv("MY_DB", "postgresql://private")

    def forbidden(*args, **kwargs):
        pytest.fail("Network mode must not manage or seed a database")

    for name in ("init", "start"):
        monkeypatch.setattr(dev.postgres, name, forbidden)
    monkeypatch.setattr(dev, "seed", forbidden)
    seen = []
    monkeypatch.setattr(dev, "serve", lambda processes, values, args: seen.append(values))
    dev.run(dev.parse_args(["--database-url-env", "MY_DB"]))
    assert seen == [{"DATABASE_URL": "postgresql://private"}]


def test_fresh_runs_unique_and_retained(workspace, monkeypatch):
    roots, starts, seeds = [], [], []
    monkeypatch.setattr(dev, "load_catalog", lambda *args: "catalog")

    def initialize(root, binaries, port, processes):
        root.mkdir()
        (root / ".env").write_text("DATABASE_URL=local\n")
        roots.append(root)
        starts.append(root)

    monkeypatch.setattr(dev.postgres, "init", initialize)
    monkeypatch.setattr(dev, "seed", lambda *args: seeds.append(args))
    monkeypatch.setattr(dev, "serve", lambda *args: None)
    dev.run(dev.parse_args([]))
    dev.run(dev.parse_args([]))
    assert len(set(roots)) == 2
    assert all(root.is_dir() for root in roots)
    assert len(starts) == len(seeds) == 2


def test_resume_preserves_data(workspace, monkeypatch):
    root = workspace / "saved"
    (root / "data").mkdir(parents=True)
    (root / "data/PG_VERSION").write_text("18")
    (root / ".env").write_text("DATABASE_URL=postgresql://localhost:55441/draft_test\n")
    monkeypatch.setattr(dev, "seed", lambda *args: pytest.fail("must not reseed"))
    monkeypatch.setattr(dev.postgres, "init", lambda *args: pytest.fail("must not initialize"))
    starts = []
    monkeypatch.setattr(dev.postgres, "start", lambda *args: starts.append(args))
    monkeypatch.setattr(dev, "serve", lambda *args: None)
    args = dev.parse_args(["--data-dir", str(root / "data"), "--db-port", "55442"])
    dev.run(args)
    assert starts[0][-1] == 55442


def test_real_output_forwarding_and_cleanup(tmp_path, capsys):
    processes = dev.Processes(tmp_path / "logs")
    child = processes.start(
        "backend",
        [
            sys.executable,
            "-u",
            "-c",
            "import sys,time; print('hello'); print('problem', file=sys.stderr); time.sleep(60)",
        ],
        cwd=tmp_path,
    )
    import time

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if (tmp_path / "logs/backend.err.log").exists() and (
            tmp_path / "logs/backend.err.log"
        ).read_text() == "problem\n":
            break
        time.sleep(0.05)
    processes.close()
    assert child.poll() is not None
    assert (tmp_path / "logs/backend.out.log").read_text() == "hello\n"
    output = capsys.readouterr().err
    assert "[backend out] hello" in output
    assert "[backend err] problem" in output


def test_failure_cleans_up_owned_processes(workspace, monkeypatch):
    monkeypatch.setenv("DB", "postgresql://private")
    closed = []
    monkeypatch.setattr(dev.Processes, "close", lambda self: closed.append(self))

    def fail(*args):
        raise RuntimeError("startup failed")

    monkeypatch.setattr(dev, "serve", fail)
    with pytest.raises(RuntimeError, match="startup failed"):
        dev.run(dev.parse_args(["--database-url-env", "DB"]))
    assert len(closed) == 1


def test_setup_command_does_not_count_as_running_server(tmp_path, monkeypatch):
    processes = dev.Processes(tmp_path / "logs")
    try:
        processes.run_command("initdb", [sys.executable, "-c", "print('setup done')"])
        assert processes.children == []
    finally:
        processes.close()
    assert (tmp_path / "logs/initdb.out.log").read_text() == "setup done\n"
