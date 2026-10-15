"""CSV validation and opt-in real PostgreSQL import/asset checks."""

import base64
import csv
import hashlib
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.import_catalog import COLUMNS, import_catalog, load_catalog  # noqa: E402

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
