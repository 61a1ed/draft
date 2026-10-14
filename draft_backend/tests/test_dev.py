"""Local launcher wiring: correct browser origin and process cleanup."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import dev  # noqa: E402


def test_child_environments_keep_credentials_out_of_frontend(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "DATABASE_URL=private-database\nSESSION_SECRET=private-session\n"
        "APP_ENV=test\nTEST_LOGIN_ENABLED=true\nPUBLIC_BASE_URL=http://127.0.0.1:8000\n"
    )
    monkeypatch.setenv("ADMIN_DATABASE_URL", "private-admin")
    monkeypatch.setenv("DATABASE_URL", "inherited-private-database")
    monkeypatch.setenv("SESSION_SECRET", "inherited-private-session")
    backend, frontend = dev.child_environments(tmp_path, 8001, 5174)
    assert backend["DATABASE_URL"] == "private-database"
    assert backend["PUBLIC_BASE_URL"] == "http://127.0.0.1:5174"
    assert backend["PORT"] == "8001"
    assert frontend["API_TARGET"] == "http://127.0.0.1:8001"
    assert frontend["VITE_ENABLE_TEST_LOGIN"] == "true"
    assert not {"ADMIN_DATABASE_URL", "DATABASE_URL", "SESSION_SECRET"} & frontend.keys()


def test_prepared_cluster_restart_preserves_draft_changes(tmp_path, monkeypatch):
    (tmp_path / dev.testdb.MARKER).touch()
    (tmp_path / dev.READY).touch()
    monkeypatch.setattr(dev.testdb, "load_marker", lambda root: {"pg_bin": str(tmp_path)})
    monkeypatch.setattr(dev.testdb, "is_running", lambda *args: False)
    started = []
    monkeypatch.setattr(dev.testdb, "start", lambda *args: started.append(args))
    monkeypatch.setattr(dev.scenarios, "apply", lambda *args: pytest.fail("must not reseed"))
    monkeypatch.setattr(dev, "import_catalog", lambda *args: pytest.fail("must not reprice"))
    dev.prepare(tmp_path, None, 55440)
    assert started == [(tmp_path, tmp_path)]


def test_first_run_imports_the_catalog_validated_before_init(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    pokemon, images = [object()], {"image": b"png"}
    actions = []

    def load(*args):
        actions.append("load")
        return pokemon, images

    def initialize(*args):
        actions.append("init")
        (tmp_path / "admin.env").write_text("ADMIN_DATABASE_URL=test\n")

    def import_catalog(url, loaded_pokemon, loaded_images):
        assert url == "test"
        assert loaded_pokemon is pokemon
        assert loaded_images is images
        actions.append("import")

    monkeypatch.setattr(dev, "load_catalog", load)
    monkeypatch.setattr(dev.testdb, "init", initialize)
    monkeypatch.setattr(dev.testdb, "load_marker", lambda root: {"pg_bin": str(tmp_path)})
    monkeypatch.setattr(dev.testdb, "is_running", lambda *args: True)
    monkeypatch.setattr(dev.scenarios, "prepare", lambda root: object())
    monkeypatch.setattr(dev.scenarios, "apply", lambda database: actions.append("seed"))
    monkeypatch.setattr(dev, "import_catalog", import_catalog)
    monkeypatch.setattr(
        dev.psycopg, "connect", lambda url: nullcontext(SimpleNamespace(execute=lambda sql: None))
    )

    dev.prepare(tmp_path, tmp_path / "bin", 55440)

    assert actions == ["load", "init", "import", "seed"]
    assert (tmp_path / dev.READY).exists()


def test_failed_second_server_cleans_up_started_children(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("APP_ENV=test\n")
    vite = tmp_path / "node_modules" / "vite" / "bin" / "vite.js"
    vite.parent.mkdir(parents=True)
    vite.touch()
    monkeypatch.setattr(dev, "FRONTEND", tmp_path)
    monkeypatch.setattr(dev.shutil, "which", lambda name: "node")
    children = []

    class Process:
        def __init__(self, command, **kwargs):
            self.stopped = False
            self.log = kwargs["stdout"]
            children.append(self)

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, **kwargs):
            assert self.stopped
            return 0

    def ready(child, url):
        if len(children) == 2:
            raise RuntimeError("frontend failed")

    monkeypatch.setattr(subprocess, "Popen", Process)
    monkeypatch.setattr(dev, "wait_ready", ready)
    with pytest.raises(RuntimeError, match="frontend failed"):
        dev.serve(tmp_path, 8000, 5173)
    assert len(children) == 2
    assert all(child.stopped and child.log.closed for child in children)
