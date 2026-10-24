"""CSV validation and opt-in real PostgreSQL import/asset checks."""

import base64
import csv
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.import_catalog import COLUMNS, import_catalog, load_catalog  # noqa: E402
from scripts.snapshot_showdown import build  # noqa: E402

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg=="
)


def source(tmp_path, rows=None):
    root = tmp_path / "images"
    root.mkdir(exist_ok=True)
    (root / "pokemon.png").write_bytes(PNG)
    path = tmp_path / "catalog.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(COLUMNS)
        writer.writerows(rows or [(25, "Pikachu", "", 10, "pokemon.png")])
    return path, root


def test_csv_forms_unicode_quoting_and_image_deduplication(tmp_path):
    path, root = source(
        tmp_path,
        [
            (25, "Pokémon, example", "", 0, "pokemon.png"),
            (25, "Pokémon, example", "Fancy", 12, "pokemon.png"),
        ],
    )
    rows, images = load_catalog(path, root)
    assert [p.name for p in rows] == ["Pokémon, example", "Pokémon, example (Fancy)"]
    assert [p.points for p in rows] == [0, 12]
    assert images == {hashlib.sha256(PNG).hexdigest(): PNG}


@pytest.mark.parametrize(
    "row",
    [
        (0, "Pikachu", "", 10, "pokemon.png"),
        (25, "Pikachu", "", -1, "pokemon.png"),
        (25, "Pikachu", "", 2147483648, "pokemon.png"),
        (25, "", "", 1, "pokemon.png"),
        (25, "Pikachu", "", 1, "missing.png"),
        (25, "Pikachu", "", 1, "../outside.png"),
        (25, "Pikachu", "", 1, "https://example.com/pokemon.png"),
        (25, "Pikachu", "", 1, "file:///pokemon.png"),
        (25, "Pikachu", "", 1, "C:\\pokemon.png"),
        (25, "Pikachu", "", 1),
    ],
)
def test_invalid_rows_fail_with_line_number(tmp_path, row):
    (tmp_path / "outside.png").write_bytes(PNG)
    path, root = source(tmp_path, [row])
    with pytest.raises(ValueError, match="CSV line 2"):
        load_catalog(path, root)


def test_duplicate_keys_and_non_png_are_rejected(tmp_path):
    row = (25, "Pikachu", "", 1, "pokemon.png")
    path, root = source(tmp_path, [row, row])
    with pytest.raises(ValueError, match="Duplicate dex/form"):
        load_catalog(path, root)
    (root / "pokemon.png").write_bytes(b"not an image")
    with pytest.raises(ValueError, match="PNG"):
        load_catalog(path, root)


STATS = {"hp": 35, "attack": 55, "defense": 40, "special_attack": 50,
         "special_defense": 50, "speed": 90}


def details(tmp_path, dex=25, **overrides):
    entry = {
        "dex": dex, "form": "", "types": ["Electric"], "abilities": ["Static"],
        "hidden_ability": "Lightning Rod", "base_stats": STATS,
        "megas": [{"name": "Mega Pikachu", "types": ["Electric", "Fairy"],
                   "abilities": ["Pixilate"], "hidden_ability": None,
                   "base_stats": {**STATS, "speed": 130}, "image_path": "pokemon.png"}],
        **overrides,
    }
    path = tmp_path / "details.json"
    path.write_text(json.dumps({"pokemon": [entry]}), encoding="utf-8")
    return path


def test_details_and_megas_are_attached_by_dex_and_form(tmp_path):
    rows, images = load_catalog(*source(tmp_path), details(tmp_path))
    pokemon = rows[0]
    assert pokemon.details.types == ("Electric",)
    assert pokemon.details.hidden_ability == "Lightning Rod"
    assert pokemon.details.stats == (35, 55, 40, 50, 50, 90)
    assert [m.name for m in pokemon.megas] == ["Mega Pikachu"]
    assert pokemon.megas[0].details.stats[-1] == 130
    assert pokemon.megas[0].image_hash == pokemon.image_hash
    assert load_catalog(*source(tmp_path))[0][0].details is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"dex": 26},
        {"types": []},
        {"types": ["Electric", "Fire", "Water"]},
        {"abilities": [""]},
        {"hidden_ability": 3},
        {"base_stats": {**STATS, "speed": 0}},
        {"base_stats": {"hp": 35}},
        {"megas": [{"name": "Mega", "types": ["Electric"], "abilities": ["A"],
                    "base_stats": STATS, "image_path": "missing.png"}]},
    ],
)
def test_invalid_details_fail_with_line_number(tmp_path, overrides):
    with pytest.raises(ValueError, match="CSV line 2"):
        load_catalog(*source(tmp_path), details(tmp_path, **overrides))


def test_snapshot_assigns_megas_to_the_matching_form(tmp_path):
    def entry(name, base=None, forme=None, **extra):
        return {"name": name, "types": ["Psychic"], "abilities": {"0": "Keen Eye", "H": "X"},
                "baseStats": {"hp": 1, "atk": 2, "def": 3, "spa": 4, "spd": 5, "spe": 6},
                **({"baseSpecies": base, "forme": forme} if base else {}), **extra}

    pokedex = {
        "meowstic": entry("Meowstic", otherFormes=["Meowstic-F", "Meowstic-M-Mega",
                                                   "Meowstic-F-Mega"]),
        "meowsticf": entry("Meowstic-F", "Meowstic", "F"),
        "meowsticmmega": entry("Meowstic-M-Mega", "Meowstic", "M-Mega"),
        "meowsticfmega": entry("Meowstic-F-Mega", "Meowstic", "F-Mega"),
        "meganium": entry("Meganium"),
        "charizard": entry("Charizard", otherFormes=["Charizard-Mega-X"]),
        "charizardmegax": entry("Charizard-Mega-X", "Charizard", "Mega-X"),
    }
    images = tmp_path / "sprites"
    images.mkdir()
    (images / "charizard-megax.png").write_bytes(PNG)
    path = tmp_path / "catalog.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(COLUMNS)
        writer.writerows([
            (678, "Meowstic", "Male", 1, "sprites/meowstic.png"),
            (678, "Meowstic", "Female", 1, "sprites/meowstic-f.png"),
            (154, "Meganium", "", 1, "sprites/meganium.png"),
            (6, "Charizard", "", 1, "sprites/charizard.png"),
        ])
    male, female, meganium, charizard = build(path, pokedex, tmp_path)
    assert [m["showdown_name"] for m in male["megas"]] == ["Meowstic-M-Mega"]
    assert [m["showdown_name"] for m in female["megas"]] == ["Meowstic-F-Mega"]
    assert female["megas"][0]["name"] == "Mega Meowstic"
    assert meganium["megas"] == []
    assert charizard["megas"][0]["name"] == "Mega Charizard X"
    assert charizard["megas"][0]["image_path"] == "sprites/charizard-megax.png"
    assert male["abilities"] == ["Keen Eye"] and male["hidden_ability"] == "X"
    assert male["base_stats"]["special_attack"] == 4


@pytest.mark.integration
def test_postgres_import_stores_and_replaces_details_and_megas(tmp_path, test_database):
    dex = 100000 + int(uuid4().hex[:6], 16)
    rows, images = load_catalog(*source(tmp_path), details(tmp_path, dex=25))
    rows = [replace(rows[0], dex=dex)]
    import_catalog(test_database.admin, rows, images)
    query = (
        "SELECT d.types, d.hidden_ability, d.speed, array_agg(m.name ORDER BY m.id) "
        "FROM dl.draftable d LEFT JOIN dl.mega_evolution m ON m.draftable_id = d.id "
        "WHERE d.dex = %s GROUP BY d.id"
    )
    with psycopg.connect(test_database.admin, autocommit=True) as conn:
        assert conn.execute(query, (dex,)).fetchone() == (
            ["Electric"], "Lightning Rod", 90, ["Mega Pikachu"],
        )
        # A reimport replaces Megas; an import without details keeps stored details.
        import_catalog(test_database.admin, [replace(rows[0], megas=())], images)
        assert conn.execute(query, (dex,)).fetchone()[3] == [None]
        import_catalog(test_database.admin, [replace(rows[0], details=None)], images)
        assert conn.execute(query, (dex,)).fetchone()[:3] == (["Electric"], "Lightning Rod", 90)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE dl.draftable SET hp = NULL WHERE dex = %s", (dex,))


@pytest.mark.integration
def test_postgres_import_repeat_update_rollback_and_read_permissions(tmp_path, test_database):
    admin, gateway = test_database.admin, test_database.gateway
    rows, images = load_catalog(*source(tmp_path))
    dex = 100000 + int(uuid4().hex[:6], 16)
    rows = [replace(rows[0], dex=dex)]
    import_catalog(admin, rows, images)
    with psycopg.connect(admin, autocommit=True) as conn:
        original = conn.execute(
            "SELECT id, point_cost, image_hash FROM dl.draftable WHERE dex = %s", (dex,)
        ).fetchone()
        import_catalog(admin, rows, images)
        assert (
            conn.execute(
                "SELECT id, point_cost, image_hash FROM dl.draftable WHERE dex = %s", (dex,)
            ).fetchone()
            == original
        )
        # A constraint failure after the image insertion rolls the entire import back.
        other = PNG + b"extra"
        other_hash = hashlib.sha256(other).hexdigest()
        with pytest.raises(psycopg.errors.CheckViolation):
            import_catalog(admin, [replace(rows[0], points=-1)], {other_hash: other})
        assert not conn.execute("SELECT FROM dl.asset WHERE hash = %s", (other_hash,)).fetchone()
        assert (
            conn.execute(
                "SELECT point_cost FROM dl.draftable WHERE id = %s", (original[0],)
            ).fetchone()[0]
            == 10
        )
        import_catalog(admin, [replace(rows[0], points=15)], images)
        assert conn.execute(
            "SELECT id, point_cost FROM dl.draftable WHERE dex = %s", (dex,)
        ).fetchone() == (original[0], 15)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO dl.asset VALUES (%s, 'image/png', %s)", ("f" * 64, PNG))
    with psycopg.connect(gateway, autocommit=True) as conn:
        conn.execute("SET ROLE dl_api")
        assert (
            conn.execute(
                "SELECT data FROM dl.asset WHERE hash = %s", (rows[0].image_hash,)
            ).fetchone()[0]
            == PNG
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM dl.asset")
