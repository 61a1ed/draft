"""Import a Pokémon CSV and local PNG images into a local or remote database."""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

import psycopg

COLUMNS = ("dex", "species", "form", "points", "image_path")


@dataclass(frozen=True)
class Pokemon:
    dex: int
    species: str
    form: str
    points: int
    image_hash: str

    @property
    def name(self) -> str:
        return f"{self.species} ({self.form})" if self.form else self.species


def load_catalog(csv_path: Path, image_root: Path) -> tuple[list[Pokemon], dict[str, bytes]]:
    """Read and validate everything before opening a database connection."""
    root = image_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Image root must be a directory")
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
                relative = row["image_path"].strip()
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
                pokemon.append(Pokemon(dex, species, form, points, digest))
            except (OSError, ValueError) as exc:
                raise ValueError(f"CSV line {reader.line_num}: {exc}") from exc
    if not pokemon:
        raise ValueError("CSV contains no Pokémon")
    return pokemon, images


def import_catalog(conninfo: str, pokemon: list[Pokemon], images: dict[str, bytes]) -> None:
    """Atomically upsert by dex/form; preserve IDs, picks, and omitted catalog rows."""
    with psycopg.connect(conninfo) as conn:
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--image-root", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true", help="Validate without database access")
    args = parser.parse_args()
    conninfo = os.environ.get("ADMIN_DATABASE_URL")
    if not args.dry_run and not conninfo:
        parser.error("Set ADMIN_DATABASE_URL to a connection allowed to SET ROLE dl_owner")
    try:
        pokemon, images = load_catalog(args.csv, args.image_root)
        if not args.dry_run:
            import_catalog(conninfo, pokemon, images)
    except (OSError, ValueError, csv.Error, psycopg.Error) as exc:
        detail = (
            f"database operation failed (SQLSTATE {exc.sqlstate or 'connection error'})"
            if isinstance(exc, psycopg.Error) else str(exc)
        )
        print(f"Import failed: {detail}", file=sys.stderr)
        raise SystemExit(1) from None
    action = "Validated" if args.dry_run else "Imported"
    print(f"{action} {len(pokemon)} Pokémon and {len(images)} unique PNG images "
          f"({sum(map(len, images.values())):,} bytes).")


if __name__ == "__main__":
    main()
