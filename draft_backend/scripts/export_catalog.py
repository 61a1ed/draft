"""Export the approved min_pokemon SQLite table to the catalog import CSV."""

import argparse
import csv
import sqlite3
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    uri = args.database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute(
            "SELECT dex, species, form, points, img_big FROM min_pokemon ORDER BY dex, form"
        ).fetchall()
    with args.output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("dex", "species", "form", "points", "image_path"))
        writer.writerows(rows)
    print(f"Exported {len(rows)} Pokémon to {args.output}.")


if __name__ == "__main__":
    main()
