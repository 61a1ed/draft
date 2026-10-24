"""Save Showdown types, abilities, base stats, and Mega Evolutions for the catalog CSV."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen

SOURCE_URL = "https://play.pokemonshowdown.com/data/pokedex.json"
STATS = {"hp": "hp", "atk": "attack", "def": "defense",
         "spa": "special_attack", "spd": "special_defense", "spe": "speed"}


def to_id(text: str) -> str:
    """Showdown's lookup key: lowercase letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def is_mega(name: str) -> bool:
    """Charizard-Mega-X and Meowstic-F-Mega are Megas; Meganium is not."""
    return "Mega" in name.split("-")


def battle_data(entry: dict) -> dict:
    abilities = entry["abilities"]
    return {
        "types": entry["types"],
        "abilities": [abilities[slot] for slot in ("0", "1", "S") if slot in abilities],
        "hidden_ability": abilities.get("H"),
        "base_stats": {name: entry["baseStats"][key] for key, name in STATS.items()},
    }


def megas_for(entry: dict, pokedex: dict, image_root: Path, sprite_dir: str) -> list[dict]:
    """Megas of the base species that belong to this form (e.g. Meowstic-F-Mega)."""
    base = pokedex[to_id(entry.get("baseSpecies", entry["name"]))]
    formes = [base["name"], *base.get("otherFormes", [])]
    result = []
    for name in filter(is_mega, formes):
        mega = pokedex[to_id(name)]
        # The longest non-Mega forme name prefixing the Mega's name owns it.
        owner = max((f for f in formes if name.startswith(f + "-") and not is_mega(f)), key=len)
        if owner != entry["name"]:
            continue
        suffix = mega["forme"].split("Mega", 1)[1].strip("-")
        sprite = f"{sprite_dir}/{to_id(mega['baseSpecies'])}-{to_id(mega['forme'])}.png"
        result.append({
            "name": f"Mega {mega['baseSpecies']}" + (f" {suffix}" if suffix else ""),
            "showdown_name": mega["name"],
            **battle_data(mega),
            "image_path": sprite if (image_root / sprite).is_file() else None,
        })
    return result


def build(csv_path: Path, pokedex: dict, image_root: Path) -> list[dict]:
    entries = []
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            # Catalog sprites use Showdown sprite names, e.g. raichu-alola.png.
            image = PurePosixPath(row["image_path"].strip().replace("\\", "/"))
            entry = pokedex.get(to_id(image.stem))
            if entry is None:
                raise ValueError(f"No Showdown entry for {row['species']} {row['form']!r}")
            entries.append({
                "dex": int(row["dex"]),
                "form": row["form"].strip(),
                "showdown_name": entry["name"],
                **battle_data(entry),
                "megas": megas_for(entry, pokedex, image_root, str(image.parent)),
            })
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--image-root", required=True, type=Path)
    parser.add_argument("--source", type=Path, help="Use a downloaded pokedex.json instead")
    args = parser.parse_args()
    try:
        if args.source:
            pokedex = json.loads(args.source.read_text(encoding="utf-8"))
        else:
            # Showdown rejects Python's default user agent.
            request = Request(SOURCE_URL, headers={"User-Agent": "draft-catalog-snapshot"})
            with urlopen(request, timeout=60) as response:
                pokedex = json.load(response)
        entries = build(args.csv, pokedex, args.image_root)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Snapshot failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    snapshot = {"source": SOURCE_URL, "pokemon": entries}
    args.output.write_text(json.dumps(snapshot, indent=1, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    megas = sum(len(e["megas"]) for e in entries)
    print(f"Saved details for {len(entries)} Pokémon and {megas} Mega Evolutions.")


if __name__ == "__main__":
    main()
