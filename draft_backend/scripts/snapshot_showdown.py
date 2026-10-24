"""Save Showdown types, abilities, base stats, Megas, and Champions moves for the catalog CSV."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen

SOURCE_URL = "https://play.pokemonshowdown.com/data/pokedex.json"
MOVES_URL = "https://play.pokemonshowdown.com/data/moves.json"
TABLES_URL = "https://play.pokemonshowdown.com/data/teambuilder-tables.js"
MOVE_TEXT_URL = (
    "https://raw.githubusercontent.com/smogon/pokemon-showdown/master/data/text/moves.ts"
)
STATS = {"hp": "hp", "atk": "attack", "def": "defense",
         "spa": "special_attack", "spd": "special_defense", "spe": "speed"}


def to_id(text: str) -> str:
    """Showdown's lookup key: lowercase letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def is_mega(name: str) -> bool:
    """Charizard-Mega-X and Meowstic-F-Mega are Megas; Meganium is not."""
    return "Mega" in name.split("-")


def fetch_text(url: str) -> str:
    """Fetch a URL using a non-default user agent accepted by Showdown."""
    request = Request(url, headers={"User-Agent": "draft-catalog-snapshot"})
    with urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8")


def parse_champions_table(js_source: str) -> dict:
    """Extract the 'champions' mod table from Showdown's teambuilder-tables.js."""
    match = re.search(r"JSON\.parse\('(.*)'\)", js_source, re.DOTALL)
    if not match:
        raise ValueError("Could not find JSON.parse payload in teambuilder-tables.js")
    raw = re.sub(r"\\(.)", lambda m: "\n" if m.group(1) == "n" else m.group(1), match.group(1))
    tables = json.loads(raw)
    champions = tables.get("champions")
    if not isinstance(champions, dict) or "learnsets" not in champions:
        raise ValueError("Missing champions learnsets in teambuilder-tables.js")
    return champions


def parse_move_descriptions(ts_source: str) -> dict[str, str]:
    """Extract top-level shortDesc (falling back to desc) from Showdown's data/text/moves.ts."""
    descriptions: dict[str, str] = {}
    current_id: str | None = None
    in_nested = False
    for line in ts_source.splitlines():
        key_match = re.match(r'^\t"?([a-z0-9]+)"?: \{$', line)
        if key_match:
            current_id = key_match.group(1)
            in_nested = False
            continue
        if current_id is None:
            continue
        if line == "\t},":
            current_id = None
            continue
        if re.match(r"^\t\t[a-z0-9]+: \{$", line):
            in_nested = True
            continue
        if in_nested:
            if line == "\t\t},":
                in_nested = False
            continue
        short_match = re.match(r'^\t\tshortDesc: "(.*)",$', line)
        if short_match:
            descriptions[current_id] = short_match.group(1).replace('\\"', '"')
        elif current_id not in descriptions:
            desc_match = re.match(r'^\t\tdesc: "(.*)",$', line)
            if desc_match:
                descriptions[current_id] = desc_match.group(1).replace('\\"', '"')
    return descriptions


def learnset_for(
    entry: dict,
    learnsets: dict[str, dict[str, str]],
    moves_index: dict[str, dict],
) -> list[str]:
    """Return sorted Champions move names for a Pokémon entry."""
    species_id = to_id(entry["name"])
    if species_id == "floette" and "floetteeternal" in learnsets:
        species_id = "floetteeternal"
    raw_moves = learnsets.get(species_id)
    if not raw_moves and "baseSpecies" in entry:
        raw_moves = learnsets.get(to_id(entry["baseSpecies"]))
    if not raw_moves:
        raise ValueError(f"No Champions learnset for {entry['name']}")
    return sorted(
        (moves_index[move_id]["name"] for move_id in raw_moves if move_id in moves_index),
        key=str.lower,
    )


def build_moves(
    moves_data: dict[str, dict],
    champions: dict,
    descriptions: dict[str, str] | None = None,
) -> dict[str, dict]:
    """Build Champions-adjusted move dictionary keyed by Showdown move ID."""
    overrides = champions.get("overrideMoveData", {})
    descriptions = descriptions or {}
    result: dict[str, dict] = {}
    for move_id, raw in moves_data.items():
        if not isinstance(raw, dict) or "name" not in raw:
            continue
        merged = {**raw, **overrides.get(move_id, {})}
        power = merged.get("basePower") or None
        acc = merged.get("accuracy")
        accuracy = None if acc is True or not acc else int(acc)
        pp_raw = merged.get("pp")
        if isinstance(pp_raw, int) and pp_raw > 0:
            # Champions caps base PP at 20 and computes max PP as (pp / 5 + 1) * 4
            # unless noPPBoosts is set.
            base_pp = min(pp_raw, 20)
            pp = base_pp if merged.get("noPPBoosts") else int((base_pp // 5 + 1) * 4)
        else:
            pp = None
        result[move_id] = {
            "name": merged["name"],
            "type": merged["type"],
            "category": merged["category"],
            "power": int(power) if isinstance(power, int) and power > 0 else None,
            "accuracy": accuracy,
            "pp": pp,
            "description": descriptions.get(move_id) or merged.get("shortDesc") or None,
        }
    return result


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


def build(
    csv_path: Path,
    pokedex: dict,
    image_root: Path,
    learnsets: dict[str, dict[str, str]] | None = None,
    moves_index: dict[str, dict] | None = None,
) -> list[dict]:
    entries = []
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            # Catalog sprites use Showdown sprite names, e.g. raichu-alola.png.
            image = PurePosixPath(row["image_path"].strip().replace("\\", "/"))
            entry = pokedex.get(to_id(image.stem))
            if entry is None:
                raise ValueError(f"No Showdown entry for {row['species']} {row['form']!r}")
            item = {
                "dex": int(row["dex"]),
                "form": row["form"].strip(),
                "showdown_name": entry["name"],
                **battle_data(entry),
                "megas": megas_for(entry, pokedex, image_root, str(image.parent)),
            }
            if learnsets is not None and moves_index is not None:
                item["moves"] = learnset_for(entry, learnsets, moves_index)
            entries.append(item)
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--image-root", required=True, type=Path)
    parser.add_argument("--source", type=Path, help="Use a downloaded pokedex.json instead")
    parser.add_argument("--moves-source", type=Path, help="Use a downloaded moves.json instead")
    parser.add_argument(
        "--tables-source", type=Path, help="Use a downloaded teambuilder-tables.js instead"
    )
    parser.add_argument(
        "--move-text-source", type=Path, help="Use a downloaded moves.ts text file instead"
    )
    args = parser.parse_args()
    try:
        pokedex = (
            json.loads(args.source.read_text(encoding="utf-8"))
            if args.source
            else json.loads(fetch_text(SOURCE_URL))
        )
        moves_data = (
            json.loads(args.moves_source.read_text(encoding="utf-8"))
            if args.moves_source
            else json.loads(fetch_text(MOVES_URL))
        )
        tables_js = (
            args.tables_source.read_text(encoding="utf-8")
            if args.tables_source
            else fetch_text(TABLES_URL)
        )
        move_text_ts = (
            args.move_text_source.read_text(encoding="utf-8")
            if args.move_text_source
            else fetch_text(MOVE_TEXT_URL)
        )
        champions = parse_champions_table(tables_js)
        descriptions = parse_move_descriptions(move_text_ts)
        moves_index = build_moves(moves_data, champions, descriptions)
        learnsets = champions["learnsets"]
        entries = build(args.csv, pokedex, args.image_root, learnsets, moves_index)
        used_names = {move_name for entry in entries for move_name in entry.get("moves", [])}
        catalog_moves = sorted(
            (move for move in moves_index.values() if move["name"] in used_names),
            key=lambda m: m["name"].lower(),
        )
    except (OSError, ValueError, KeyError) as exc:
        print(f"Snapshot failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    snapshot = {"source": SOURCE_URL, "moves": catalog_moves, "pokemon": entries}
    args.output.write_text(json.dumps(snapshot, indent=1, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    megas = sum(len(e["megas"]) for e in entries)
    print(
        f"Saved details for {len(entries)} Pokémon, {megas} Mega Evolutions, "
        f"and {len(catalog_moves)} moves."
    )


if __name__ == "__main__":
    main()
