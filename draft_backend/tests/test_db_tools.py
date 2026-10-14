"""Safety and subprocess-boundary checks; real SQL is covered by integration tests."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Administrative scripts intentionally live outside the installed server package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import testdb  # noqa: E402
from scripts.bootstrap import schema_body  # noqa: E402


def mark(root: Path, pg_bin: Path) -> None:
    root.mkdir()
    (root / "data").mkdir()
    (root / "data" / "PG_VERSION").write_text("18\n")
    (root / testdb.MARKER).write_text(
        json.dumps(
            {
                "format": testdb.MAGIC,
                "root": str(root.resolve()),
                "data": str(root.resolve() / "data"),
                "pg_bin": str(pg_bin.resolve()),
                "database": testdb.DATABASE,
                "port": 55432,
            }
        )
    )


def test_schema_wrapper_only_removed_in_memory():
    path = testdb.PROJECT / "sql" / "schema.sql"
    original = path.read_bytes()
    body = schema_body()
    assert "\nBEGIN;\n" not in body
    assert "\nCOMMIT;\n" not in body
    assert "BEGIN\n    PERFORM admit" in body
    assert path.read_bytes() == original


def test_init_refuses_nonempty_root(tmp_path, monkeypatch):
    (tmp_path / "important.txt").write_text("keep")
    monkeypatch.setattr(testdb, "run", lambda *a, **kw: pytest.fail("must not run binaries"))
    with pytest.raises(RuntimeError, match="empty root"):
        testdb.init(tmp_path, tmp_path / "bin", 55432)
    assert (tmp_path / "important.txt").read_text() == "keep"


@pytest.mark.parametrize(
    "marker_kind", ["absent", "invalid_json", "invalid_shape", "different_root"]
)
def test_destroy_refuses_unrecognized_root(tmp_path, monkeypatch, marker_kind):
    root = tmp_path / "cluster"
    root.mkdir()
    (root / "important.txt").write_text("keep")
    if marker_kind == "invalid_json":
        (root / testdb.MARKER).write_text("not json")
    elif marker_kind == "invalid_shape":
        (root / testdb.MARKER).write_text("[]")
    elif marker_kind == "different_root":
        (root / testdb.MARKER).write_text(
            json.dumps(
                {
                    "format": testdb.MAGIC,
                    "root": str(tmp_path / "other"),
                }
            )
        )
    monkeypatch.setattr(sys, "argv", ["testdb.py", "--root", str(root), "destroy"])
    monkeypatch.setattr(testdb, "run", lambda *a, **kw: pytest.fail("must not run binaries"))
    monkeypatch.setattr(testdb.shutil, "rmtree", lambda *a: pytest.fail("must not delete"))
    with pytest.raises(SystemExit) as exc:
        testdb.main()
    assert exc.value.code == 1
    assert (root / "important.txt").read_text() == "keep"


@pytest.mark.parametrize("stop_fails", [False, True])
def test_destroy_must_stop_running_cluster_first(tmp_path, monkeypatch, stop_fails):
    root = tmp_path / "cluster"
    mark(root, tmp_path / "bin")
    actions = []

    def fake_run(pg_bin, name, *args, **kwargs):
        actions.append(args[0])
        if args[0] == "stop" and stop_fails:
            raise subprocess.CalledProcessError(1, [name, *args])
        return subprocess.CompletedProcess([name, *args], 0)

    monkeypatch.setattr(testdb, "run", fake_run)
    monkeypatch.setattr(testdb.shutil, "rmtree", lambda path: actions.append(("delete", path)))
    monkeypatch.setattr(sys, "argv", ["testdb.py", "--root", str(root), "destroy"])
    if stop_fails:
        with pytest.raises(SystemExit):
            testdb.main()
        assert actions == ["status", "stop"]
    else:
        testdb.main()
        assert actions == ["status", "stop", ("delete", root)]


def test_windows_command_keeps_paths_as_arguments(tmp_path, monkeypatch):
    pg_bin = tmp_path / "Program Files" / "PostgreSQL 18" / "bin"
    pg_bin.mkdir(parents=True)
    executable = pg_bin / "initdb.exe"
    executable.touch()
    password_path = tmp_path / "test cluster" / "password file"
    calls = []

    def fake_subprocess(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(testdb, "os", SimpleNamespace(name="nt", environ={"PGPASSWORD": "secret"}))
    monkeypatch.setattr(testdb.subprocess, "run", fake_subprocess)
    testdb.run(pg_bin, "initdb", "--pwfile", str(password_path))
    command, options = calls[0]
    assert command == [str(executable), "--pwfile", str(password_path)]
    assert not options.get("shell", False)
    assert "PGPASSWORD" not in options["env"]
    assert options["check"] is True


def test_failed_init_removes_temporary_password(tmp_path, monkeypatch):
    root = tmp_path / "cluster"
    pg_bin = tmp_path / "bin"
    monkeypatch.setattr(testdb, "binary", lambda base, name: base / name)
    if os.name != "nt":
        monkeypatch.setattr(testdb.os, "geteuid", lambda: 1000)

    def fake_run(base, name, *args, **kwargs):
        if args == ("--version",):
            return subprocess.CompletedProcess([name, *args], 0)
        password_file = root / ".initdb-password"
        assert password_file.exists()
        if os.name != "nt":
            assert password_file.stat().st_mode & 0o777 == 0o600
        raise subprocess.CalledProcessError(1, [name, *args])

    monkeypatch.setattr(testdb, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        testdb.init(root, pg_bin, 55432)
    assert not (root / ".initdb-password").exists()
    assert testdb.load_marker(root)["format"] == testdb.MAGIC
