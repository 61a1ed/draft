"""Import a Pokémon CSV, local PNG images, and optional Showdown details into a database."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

import psycopg

COLUMNS = ("dex", "species", "form", "points", "image_path")
STATS = ("hp", "attack", "defense", "special_attack", "special_defense", "speed")
# The deployment initializer takes this same database-scoped transaction lock.
CATALOG_LOCK = 0x444C434154414C4F


def lock_catalog(conn: psycopg.Connection) -> None:
    """Serialize catalog/schema work without tying ownership to a host machine."""
    row = conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (CATALOG_LOCK,)).fetchone()
    if not row or not row[0]:
        raise RuntimeError("Another database initialization or catalog import is running")


@dataclass(frozen=True)
class Details:
    types: tuple[str, ...]
    abilities: tuple[str, ...]
    hidden_ability: str | None
    stats: tuple[int, ...]  # In STATS order.

    @property
    def values(self) -> tuple:
        return (list(self.types), list(self.abilities), self.hidden_ability, *self.stats)


@dataclass(frozen=True)
class Mega:
    name: str
    details: Details
    image_hash: str | None


@dataclass(frozen=True)
class Pokemon:
    dex: int
    species: str
    form: str
    points: int
    image_hash: str
    details: Details | None = None
    megas: tuple[Mega, ...] = ()

    @property
    def name(self) -> str:
        return f"{self.species} ({self.form})" if self.form else self.species


def read_png(root: Path, relative: str, images: dict[str, bytes]) -> str:
    """Add a PNG beneath root to images and return its SHA-256 hash."""
    relative = relative.strip()
    if not relative or ":" in relative or PureWindowsPath(relative).anchor:
        raise ValueError("Image path must be relative; URLs are not supported")
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Image must be a file beneath the image root")
    data = path.read_bytes()
    # Accept the PNG format used by this catalog; no conversion or network fetch.
    if (not data.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
            or not data.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")):
        raise ValueError("Image must have a PNG header and end marker")
    digest = hashlib.sha256(data).hexdigest()
    images[digest] = data
    return digest


def names(value, label: str, limit: int) -> tuple[str, ...]:
    if (not isinstance(value, list) or not 1 <= len(value) <= limit
            or not all(isinstance(v, str) and v.strip() for v in value)):
        raise ValueError(f"{label} must be a list of 1 to {limit} names")
    return tuple(v.strip() for v in value)


def parse_details(entry: dict) -> Details:
    hidden, stats = entry.get("hidden_ability"), entry.get("base_stats")
    if hidden is not None and not (isinstance(hidden, str) and hidden.strip()):
        raise ValueError("Hidden ability must be a name or null")
    if not isinstance(stats, dict) or not all(
        type(stats.get(stat)) is int and 1 <= stats[stat] <= 255 for stat in STATS
    ):
        raise ValueError("Base stats must include " + ", ".join(STATS) + " from 1 to 255")
    return Details(
        names(entry.get("types"), "Types", 2),
        names(entry.get("abilities"), "Abilities", 3),
        hidden and hidden.strip(),
        tuple(stats[stat] for stat in STATS),
    )


def load_details(path: Path) -> dict[tuple[int, str], dict]:
    """Index a snapshot_showdown.py file by the CSV's dex/form key."""
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("pokemon") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise ValueError("Details file must contain a list of Pokémon")
    return {(e.get("dex"), str(e.get("form", "")).strip()): e for e in entries}


def load_catalog(
    csv_path: Path, image_root: Path, details_path: Path | None = None
) -> tuple[list[Pokemon], dict[str, bytes]]:
    """Read and validate everything before opening a database connection."""
    root = image_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Image root must be a directory")
    known = load_details(details_path) if details_path else None
    pokemon, images, seen = [], {}, set()
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames is None or sorted(reader.fieldnames) != sorted(COLUMNS):
            raise ValueError("CSV columns must be: " + ", ".join(COLUMNS))
        for row in reader:
            try:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("Incorrect number of columns")
                if any("\x00" in value for value in row.values()):
                    raise ValueError("NUL characters are not allowed")
                dex, points = int(row["dex"]), int(row["points"])
                if not 1 <= dex <= 2147483647 or not 0 <= points <= 2147483647:
                    raise ValueError("Dex must be positive and points nonnegative, within int32")
                species, form = row["species"].strip(), row["form"].strip()
                if not species:
                    raise ValueError("Species is required")
                key = (dex, form)
                if key in seen:
                    raise ValueError(f"Duplicate dex/form: {key}")
                seen.add(key)
                digest = read_png(root, row["image_path"], images)
                details, megas = None, ()
                if known is not None:
                    entry = known.get(key)
                    if entry is None:
                        raise ValueError(f"No Showdown details for dex/form {key}")
                    details = parse_details(entry)
                    raw_megas = entry.get("megas", [])
                    if not isinstance(raw_megas, list) or not all(
                        isinstance(m, dict) for m in raw_megas
                    ):
                        raise ValueError("Megas must be a list")
                    megas = tuple(
                        Mega(
                            names([m.get("name")], "Mega name", 1)[0],
                            parse_details(m),
                            read_png(root, m["image_path"], images) if m.get("image_path")
                            else None,
                        )
                        for m in raw_megas
                    )
                    if len({m.name for m in megas}) != len(megas):
                        raise ValueError("Duplicate Mega name")
                pokemon.append(Pokemon(dex, species, form, points, digest, details, megas))
            except (OSError, ValueError) as exc:
                raise ValueError(f"CSV line {reader.line_num}: {exc}") from exc
    if not pokemon:
        raise ValueError("CSV contains no Pokémon")
    return pokemon, images


def import_catalog(conninfo: str, pokemon: list[Pokemon], images: dict[str, bytes]) -> None:
    """Atomically upsert by dex/form; preserve IDs, picks, and omitted catalog rows."""
    with psycopg.connect(conninfo) as conn:
        import_catalog_connection(conn, pokemon, images)


def import_catalog_connection(
    conn: psycopg.Connection, pokemon: list[Pokemon], images: dict[str, bytes]
) -> None:
    """Import on a caller-owned connection so deployment checks share its lock."""
    with conn.transaction():
        lock_catalog(conn)
        conn.execute("SET LOCAL ROLE dl_owner")
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO dl.asset (hash, content_type, data) VALUES (%s, 'image/png', %s) "
                "ON CONFLICT (hash) DO NOTHING",
                list(images.items()),
            )
            cursor.executemany(
                "INSERT INTO dl.draftable (dex, species, form, name, point_cost, image_hash) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (dex, form) DO UPDATE SET species = EXCLUDED.species, "
                "name = EXCLUDED.name, point_cost = EXCLUDED.point_cost, "
                "image_hash = EXCLUDED.image_hash",
                [(p.dex, p.species, p.form, p.name, p.points, p.image_hash) for p in pokemon],
            )
            # Rows imported without details keep any details stored earlier.
            detailed = [p for p in pokemon if p.details]
            detail_columns = ", ".join(("types", "abilities", "hidden_ability", *STATS))
            placeholders = ", ".join(["%s"] * (3 + len(STATS)))
            cursor.executemany(
                f"UPDATE dl.draftable SET ({detail_columns}) = ROW({placeholders}) "
                "WHERE dex = %s AND form = %s",
                [(*p.details.values, p.dex, p.form) for p in detailed],
            )
            cursor.executemany(
                "DELETE FROM dl.mega_evolution m USING dl.draftable d "
                "WHERE m.draftable_id = d.id AND d.dex = %s AND d.form = %s",
                [(p.dex, p.form) for p in detailed],
            )
            cursor.executemany(
                f"INSERT INTO dl.mega_evolution (draftable_id, name, {detail_columns}, "
                f"image_hash) SELECT id, %s, {placeholders}, %s FROM dl.draftable "
                "WHERE dex = %s AND form = %s",
                [
                    (m.name, *m.details.values, m.image_hash, p.dex, p.form)
                    for p in detailed for m in p.megas
                ],
            )
        conn.execute("RESET ROLE")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--image-root", required=True, type=Path)
    parser.add_argument(
        "--details", type=Path, help="Showdown details JSON from scripts/snapshot_showdown.py"
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate without database access")
    args = parser.parse_args()
    conninfo = os.environ.get("ADMIN_DATABASE_URL")
    if not args.dry_run and not conninfo:
        parser.error("Set ADMIN_DATABASE_URL to a connection allowed to SET ROLE dl_owner")
    try:
        pokemon, images = load_catalog(args.csv, args.image_root, args.details)
        if not args.dry_run:
            import_catalog(conninfo, pokemon, images)
    except (OSError, ValueError, RuntimeError, csv.Error, psycopg.Error) as exc:
        detail = (
            f"database operation failed (SQLSTATE {exc.sqlstate or 'connection error'})"
            if isinstance(exc, psycopg.Error) else str(exc)
        )
        print(f"Import failed: {detail}", file=sys.stderr)
        raise SystemExit(1) from None
    action = "Validated" if args.dry_run else "Imported"
    megas = sum(len(p.megas) for p in pokemon)
    print(f"{action} {len(pokemon)} Pokémon, {megas} Mega Evolutions, and {len(images)} unique "
          f"PNG images ({sum(map(len, images.values())):,} bytes).")


if __name__ == "__main__":
    main()
