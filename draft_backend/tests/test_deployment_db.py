"""Deployment connection isolation, incomplete-state protection, and mTLS checks."""

import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from scripts import deployment_db as deployment

REVISION = "a" * 40


@pytest.fixture
def environment(tmp_path):
    result = {"PGHOST": "127.0.0.1", "PGPORT": "5432", "PGSSLMODE": "verify-ca"}
    for key in ("PGSSLROOTCERT", "PGSSLCERT", "PGSSLKEY"):
        path = tmp_path / key
        path.write_text("placeholder", encoding="utf-8")
        result[key] = str(path)
    return result


def test_connection_requires_explicit_ca_validation_and_certificate_files(environment):
    options = deployment.connection_options(environment)
    assert options["sslmode"] == "verify-ca"
    assert options["sslcertmode"] == "require"
    assert options["gssencmode"] == "disable"
    assert options["password"] == ""
    assert options["passfile"] == os.devnull
    for mode in ("require", "prefer", "verify-full", "disable"):
        with pytest.raises(ValueError, match="verify-ca"):
            deployment.connection_options({**environment, "PGSSLMODE": mode})
    with pytest.raises(ValueError, match="existing file"):
        deployment.connection_options({**environment, "PGSSLCERT": "missing-client-certificate"})
    with pytest.raises(ValueError, match="explicitly"):
        deployment.connection_options({key: value for key, value in environment.items()
                                       if key != "PGHOST"})


@pytest.mark.parametrize("host", ["/tmp/socket", "@socket", "host1,host2", "host\n"])
def test_tcp_target_cannot_fall_back_to_socket_or_second_host(environment, host):
    with pytest.raises(ValueError, match="one TCP host"):
        deployment.connection_options({**environment, "PGHOST": host})


def test_cli_scrubs_ambient_libpq_settings_and_sanitizes_connection_errors(
    environment, monkeypatch, capsys
):
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    for key in ("PGSERVICE", "PGHOSTADDR", "PGUSER", "PGPASSWORD", "PGOPTIONS"):
        monkeypatch.setenv(key, "ambient-secret")
    monkeypatch.setenv("DL_GATEWAY_PASSWORD", "gateway-secret")

    def fail(options, name, revision, password):
        assert not any(key.upper().startswith("PG") for key in os.environ)
        assert password == "gateway-secret"
        assert options["host"] == "127.0.0.1"
        raise psycopg.OperationalError("connection leaked password=gateway-secret")

    monkeypatch.setattr(deployment, "initialize", fail)
    with pytest.raises(SystemExit) as exit_info:
        deployment.main(["init", "--name", "test", "--revision", REVISION])
    assert exit_info.value.code == 1
    error = capsys.readouterr().err
    assert "connection error" in error
    assert "secret" not in error


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.queries = []
        self.info = SimpleNamespace(server_version=160000)

    def execute(self, query, args=None):
        self.queries.append((query, args))
        return Result(next(self.responses))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


@pytest.mark.parametrize("owner", [None, "dl_owner"])
def test_unmarked_or_forged_marker_is_not_reused(owner):
    conn = Connection([[] if owner is None else [(123, owner, owner)]])
    with pytest.raises(RuntimeError, match="unmarked or partially"):
        deployment.check_marker(conn, "test", REVISION)


@pytest.mark.parametrize("rows", [[], [("other", REVISION, 1)], [("test", "invalid", 1)]])
def test_marker_requires_identity_valid_revision_and_single_completion(rows):
    conn = Connection([[(123, "postgres", "postgres")], rows])
    with pytest.raises(RuntimeError, match="does not match"):
        deployment.check_marker(conn, "test", REVISION)


def test_marker_keeps_bootstrap_provenance_but_allows_new_code_revision():
    conn = Connection([[(123, "postgres", "postgres")], [("test", "b" * 40, 1)]])
    deployment.check_marker(conn, "test", REVISION)


def test_reserved_roles_prevent_database_creation(monkeypatch):
    conn = Connection([[('postgres', True, 'CN=postgres')], [(True,)], [], [("dl_importer",)]])
    monkeypatch.setattr(deployment.psycopg, "connect", lambda **kwargs: conn)
    with pytest.raises(RuntimeError, match="Reserved application roles"):
        deployment.initialize({}, "test", REVISION, "secret")
    assert not any("CREATE" in query for query, _ in conn.queries)


def test_existing_unmarked_database_is_never_bootstrapped(monkeypatch):
    maintenance = Connection([[('postgres', True, 'CN=postgres')], [(True,)], [(1,)]])
    app = Connection([[('postgres', True, 'CN=postgres')], [(True,)], []])
    connections = iter([maintenance, app])
    monkeypatch.setattr(deployment.psycopg, "connect", lambda **kwargs: next(connections))
    with pytest.raises(RuntimeError, match="unmarked or partially"):
        deployment.initialize({}, "test", REVISION, "secret")
    assert not any("CREATE" in query for query, _ in maintenance.queries + app.queries)


@pytest.mark.parametrize("missing,message", [
    (True, "connection requires a valid client certificate"),
    (False, "SSL error: tlsv1 alert unknown ca"),
    (False, "certificate authentication failed for user postgres"),
])
def test_certificate_negative_checks_require_server_rejection(monkeypatch, missing, message):
    def reject(**kwargs):
        raise psycopg.OperationalError(message)

    monkeypatch.setattr(deployment.psycopg, "connect", reject)
    deployment.rejected_certificate({}, missing=missing)


@pytest.mark.parametrize("message", [
    "connection timed out", "could not read private key file", "certificate verify failed",
    "connection refused", "invalid connection option sslcertmode",
])
def test_local_tls_errors_and_outages_do_not_pass_negative_tests(monkeypatch, message):
    def reject(**kwargs):
        raise psycopg.OperationalError(message)

    monkeypatch.setattr(deployment.psycopg, "connect", reject)
    with pytest.raises(RuntimeError, match="could not be verified"):
        deployment.rejected_certificate({}, missing=False)


def test_missing_certificate_must_not_be_accepted(monkeypatch):
    monkeypatch.setattr(deployment.psycopg, "connect", lambda **kwargs: nullcontext())
    with pytest.raises(RuntimeError, match="Server accepted"):
        deployment.rejected_certificate({}, missing=True)


def test_verification_disables_client_cert_and_rechecks_trusted_after_failures(
    environment, monkeypatch
):
    options = deployment.connection_options(environment)
    calls = []

    def connect(**kwargs):
        calls.append(kwargs)
        if kwargs.get("sslcertmode") == "disable":
            raise psycopg.OperationalError("connection requires a valid client certificate")
        if kwargs["sslcert"] != options["sslcert"]:
            raise psycopg.OperationalError("tlsv1 alert unknown ca")
        return Connection([[('postgres', True, 'CN=postgres')]])

    monkeypatch.setattr(deployment.psycopg, "connect", connect)
    deployment.verify(options, "test", REVISION,
                      Path(environment["PGSSLROOTCERT"]), Path(environment["PGSSLKEY"]))
    assert len(calls) == 4
    assert calls[0] == calls[-1]
    assert calls[1]["sslcertmode"] == "disable"
    assert all(call["sslmode"] == "verify-ca" for call in calls)
    assert all(call["dbname"] == "postgres" for call in calls)


def test_import_refuses_busy_initialization_before_reading_marker(environment, monkeypatch):
    conn = Connection([[('dl_importer', True, 'CN=dl_importer')], [(False,)]])
    monkeypatch.setattr(deployment, "load_catalog", lambda *args: ([], {}))
    monkeypatch.setattr(deployment.psycopg, "connect", lambda **kwargs: conn)
    with pytest.raises(RuntimeError, match="Another database initialization or catalog import"):
        deployment.import_data({}, "test", REVISION, Path("catalog.csv"), Path("images"))
    assert not any("completion" in query for query, _ in conn.queries)
