"""Save a bar chart of how many Pokémon exist at each point value in a catalog CSV."""

import argparse
import csv
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent
COLUMNS = ("Drafteon Points", "Beaz Rec")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path, nargs="?",
                        default=HERE / "regulation_m-c_eligible_pokemon.csv")
    parser.add_argument("--output", type=Path, default=HERE / "points_chart.png")
    args = parser.parse_args()
    with args.csv.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    counts = {column: Counter(int(row[column]) for row in rows) for column in COLUMNS}
    points = sorted(set().union(*counts.values()))
    width = 0.8 / len(COLUMNS)
    fig, ax = plt.subplots(figsize=(max(8, len(points) * 0.7), 5))
    for i, column in enumerate(COLUMNS):
        offset = (i - (len(COLUMNS) - 1) / 2) * width
        bars = ax.bar([p + offset for p in points], [counts[column][p] for p in points],
                      width, label=column)
        ax.bar_label(bars, fontsize=7)
    ax.set_xticks(points)
    ax.set(xlabel="Point value", ylabel="Number of Pokémon",
           title=f"Pokémon per point value ({len(rows)} total)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(args.output, dpi=150)
    print(f"Saved {args.output}")


if __name__ == "__main__":
    main()
