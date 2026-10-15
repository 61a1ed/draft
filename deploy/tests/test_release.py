import io
import json
import tarfile
from types import SimpleNamespace

import pytest

from host import release

A = "a" * 40
B = "b" * 40


@pytest.fixture
def releases(tmp_path):
    controller = release.Releases(
        {"name": "draft", "hostname": "example.com", "callback_path": "/g"}
    )
    controller.root = tmp_path
    controller.releases = tmp_path / "releases"
    controller.config = tmp_path / "caddy.json"
    controller.config.write_text(
        json.dumps(
            {
                "admin": {"listen": "127.0.0.1:2019"},
                "apps": {
                    "http": {
                        "servers": {
                            "public": {
                                "routes": [
                                    {"match": [{"host": ["example.com"]}], "handle": []},
                                    {"match": [{"host": ["unrelated.example"]}], "handle": []},
                                ]
                            }
                        }
                    }
                },
            }
        )
    )
    return controller


def test_candidate_switches_frontend_and_api_together(releases):
    config = json.loads(releases.candidate_config(B))
    routes = config["apps"]["http"]["servers"]["public"]["routes"]
    handlers = routes[0]["handle"][0]["routes"]
    assert "/g" in handlers[0]["match"][0]["path"]
    assert "/ready" in handlers[0]["match"][0]["path"]
    assert handlers[0]["handle"][0]["upstreams"][0]["dial"] == f"unix//run/draft-{B}/http.sock"
    assert handlers[2]["handle"][1]["root"] == str(releases.releases / B / "draft_frontend/dist")
    assert handlers[1]["handle"][2]["root"] == str(releases.root / "assets")
    assert routes[1]["match"] == [{"host": ["unrelated.example"]}]
    assert config["admin"]["listen"] == "127.0.0.1:2019"


@pytest.mark.parametrize("failure", ["prepare", "start", "ready", "enable", "load", "persist"])
def test_failed_activation_never_retires_previous(releases, monkeypatch, failure):
    events = []
    original = releases.config.read_bytes()

    def step(name):
        def operation(*args):
            events.append(name)
            if failure == name:
                raise RuntimeError("injected failure")

        return operation

    def systemctl(*args, **kwargs):
        assert args[0] == "systemctl"
        assert args[1] in {"start", "enable"}
        assert args[2] == releases.unit(B)
        step(args[1])()

    monkeypatch.setattr(releases, "prepare", step("prepare"))
    monkeypatch.setattr(release, "run", systemctl)
    monkeypatch.setattr(releases, "wait_ready", step("ready"))
    monkeypatch.setattr(releases, "load_caddy", step("load"))
    monkeypatch.setattr(release, "atomic_write", step("persist"))
    monkeypatch.setattr(releases, "retire", step("retire"))
    with pytest.raises(RuntimeError, match="injected"):
        releases.activate(B)
    assert "retire" not in events
    assert releases.config.read_bytes() == original
    if failure in {"prepare", "start", "ready"}:
        assert "enable" not in events
    if failure in {"prepare", "start", "ready", "enable"}:
        assert "load" not in events
        assert "persist" not in events
    else:
        assert events.index("enable") < events.index("load")


def test_success_persists_before_retiring(releases, monkeypatch):
    events = []
    monkeypatch.setattr(releases, "prepare", lambda revision: events.append("prepare"))
    monkeypatch.setattr(release, "run", lambda *args, **kwargs: events.append(args[1]))
    monkeypatch.setattr(releases, "wait_ready", lambda revision: events.append("ready"))
    monkeypatch.setattr(releases, "load_caddy", lambda data: events.append("load"))
    monkeypatch.setattr(release, "atomic_write", lambda *args: events.append("persist"))
    monkeypatch.setattr(releases, "retire", lambda revision: events.append("retire"))
    releases.activate(B)
    assert events == ["prepare", "start", "ready", "enable", "load", "persist", "retire"]


def test_cleanup_failure_keeps_release_active_and_shows_traceback(releases, monkeypatch, capsys):
    monkeypatch.setattr(releases, "prepare", lambda revision: None)
    monkeypatch.setattr(release, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(releases, "wait_ready", lambda revision: None)
    monkeypatch.setattr(releases, "load_caddy", lambda data: None)
    monkeypatch.setattr(release, "atomic_write", lambda path, data: path.write_bytes(data))
    candidate = releases.candidate_config(B)

    def fail_retire(revision):
        raise OSError("old-service cleanup diagnostic detail")

    monkeypatch.setattr(releases, "retire", fail_retire)
    releases.activate(B)

    assert releases.config.read_bytes() == candidate
    output = capsys.readouterr()
    assert "release is active" in output.out
    assert "Traceback (most recent call last):" in output.err
    assert "in fail_retire" in output.err
    assert "OSError: old-service cleanup diagnostic detail" in output.err


def test_asset_collision_preserves_previous_bytes(releases, monkeypatch):
    directory = releases.releases / B
    assets = directory / "draft_frontend/dist/assets"
    assets.mkdir(parents=True)
    (assets.parent / "index.html").write_text("<html></html>")
    (assets / "app-hash.js").write_bytes(b"candidate")
    shared = releases.root / "assets"
    shared.mkdir()
    (shared / "app-hash.js").write_bytes(b"previous")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        archive.add(assets / "app-hash.js", arcname="app-hash.js")
    monkeypatch.setattr(releases, "asset_archive", lambda directory: buffer.getvalue())
    with pytest.raises(RuntimeError, match="collision"):
        releases.publish_assets(directory)
    assert (shared / "app-hash.js").read_bytes() == b"previous"


def test_root_does_not_follow_symlinks_in_asset_archive(releases, monkeypatch):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo("leaked-key")
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/ssh/ssh_host_rsa_key"
        archive.addfile(member)
    monkeypatch.setattr(releases, "asset_archive", lambda directory: buffer.getvalue())
    with pytest.raises(RuntimeError, match="archive entry"):
        releases.publish_assets(releases.releases / B)
    assert not (releases.root / "assets/leaked-key").exists()


def test_retirement_includes_enabled_but_unloaded_units(releases, monkeypatch):
    stopped = []

    def fake_run(*args, **kwargs):
        if args[1] == "list-unit-files":
            return SimpleNamespace(stdout=f"draft@{A}.service enabled\n")
        return SimpleNamespace(stdout=f"draft@{B}.service loaded active running\n")

    monkeypatch.setattr(release, "run", fake_run)
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda args: stopped.append(args) or SimpleNamespace(returncode=0),
    )
    releases.retire(B)
    assert stopped == [["systemctl", "disable", "--now", f"draft@{A}.service"]]


def test_staging_refuses_an_unidentified_existing_directory(releases):
    (releases.releases / A).mkdir(parents=True)
    with pytest.raises(RuntimeError, match="identity"):
        releases.stage(A)


@pytest.mark.parametrize("value", ["HEAD", "a" * 39, "../bad", "A" * 40])
def test_only_full_commit_ids(value):
    with pytest.raises(ValueError):
        release.revision_id(value)
