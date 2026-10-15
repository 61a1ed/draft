"""Server entry point selects a private production socket or a local TCP listener."""

from pathlib import Path
from unittest.mock import Mock

from draft_backend import __main__ as server


def test_systemd_listener_uses_socket_and_trusts_only_its_restricted_transport(monkeypatch):
    monkeypatch.setenv("RUNTIME_DIRECTORY", "/run/draft-release")
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "ignored-for-socket")
    run = Mock()
    monkeypatch.setattr(server.uvicorn, "run", run)
    server.main()
    args = run.call_args.kwargs
    assert args["uds"] == str(Path("/run/draft-release") / "http.sock")
    assert "host" not in args and "port" not in args
    assert args["proxy_headers"] is True
    assert args["forwarded_allow_ips"] == "*"
    assert args["timeout_graceful_shutdown"] == 5
    assert args["http"] == "h11"
    assert args["loop"] == "asyncio:SelectorEventLoop"
    assert args["factory"] is True
    assert args["workers"] == 1


def test_local_listener_retains_tcp_settings_without_trusting_proxy_headers(monkeypatch):
    monkeypatch.delenv("RUNTIME_DIRECTORY", raising=False)
    monkeypatch.setenv("HOST", "127.0.0.2")
    monkeypatch.setenv("PORT", "8123")
    run = Mock()
    monkeypatch.setattr(server.uvicorn, "run", run)
    server.main()
    args = run.call_args.kwargs
    assert args["host"] == "127.0.0.2"
    assert args["port"] == 8123
    assert "uds" not in args
    assert args["proxy_headers"] is False
    assert args["forwarded_allow_ips"] == "127.0.0.1"
