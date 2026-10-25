"""Deployment connection isolation, incomplete-state protection, and mTLS checks."""

import base64
import json
import os
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


def test_import_refuses_busy_initialization_before_reading_marker(environment, monkeypatch):
    conn = Connection([[('dl_importer', True, 'CN=dl_importer')], [(False,)]])
    monkeypatch.setattr(deployment, "load_catalog", lambda *args: ([], {}))
    monkeypatch.setattr(deployment.psycopg, "connect", lambda **kwargs: conn)
    with pytest.raises(RuntimeError, match="Another database initialization or catalog import"):
        deployment.import_data({}, "test", REVISION, Path("catalog.csv"), Path("images"))
    assert not any("completion" in query for query, _ in conn.queries)


def test_import_cli_loads_details_and_moves_before_writing(environment, tmp_path, monkeypatch):
    csv_path = tmp_path / "catalog.csv"
    csv_path.write_text("dex,species,form,points,image_path\n25,Pikachu,,10,pokemon.png\n")
    (tmp_path / "pokemon.png").write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg=="
    ))
    details_path = tmp_path / "details.json"
    details_path.write_text(json.dumps({
        "pokemon": [{
            "dex": 25, "form": "", "types": ["Electric"], "abilities": ["Static"],
            "hidden_ability": "Lightning Rod",
            "base_stats": {"hp": 35, "attack": 55, "defense": 40, "special_attack": 50,
                           "special_defense": 50, "speed": 90},
            "moves": ["Thunderbolt"],
        }],
        "moves": [{"name": "Thunderbolt", "type": "Electric", "category": "Special",
                   "power": 90, "accuracy": 100, "pp": 16, "description": "May paralyze."}],
    }))
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    conn = Connection([
        [('dl_importer', True, 'CN=dl_importer')], [(True,)],
        [(123, "postgres", "postgres")], [("test", REVISION, 1)],
    ])
    monkeypatch.setattr(deployment.psycopg, "connect", lambda **kwargs: conn)
    imported = []
    monkeypatch.setattr(
        deployment, "import_catalog_connection",
        lambda conn, pokemon, images: imported.extend(pokemon),
    )
    deployment.main([
        "import", "--name", "test", "--revision", REVISION,
        "--csv", str(csv_path), "--image-root", str(tmp_path), "--details", str(details_path),
    ])
    assert imported[0].details.types == ("Electric",)
    assert imported[0].details.hidden_ability == "Lightning Rod"
    assert imported[0].moves[0].name == "Thunderbolt"
    assert imported[0].moves[0].power == 90
