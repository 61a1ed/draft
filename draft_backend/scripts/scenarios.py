"""Load editable SQL scenarios into a marked, disposable local test database."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

if __package__:
    from .bootstrap import PROJECT
    from .postgres import read_environment
else:
    from bootstrap import PROJECT
    from postgres import read_environment

SCENARIO_DIR = PROJECT / "sql" / "scenarios"
SCENARIOS = {
    "setup": "Three teams; draft order still needs configuring.",
    "ready": "Three ordered teams; ready for Alice to start.",
    "live": "Four picks into a snake draft; Bob picks next.",
    "budget": "Bob has zero points; Alice can waive the cost of a paid entry.",
    "undo": "An undone pick and its replacement; Carol picks next.",
    "completed": "A finished two-round draft; Alice can undo the final pick.",
}


@dataclass(frozen=True)
class ScenarioDatabase:
    root: Path
    admin_dsn: str = field(repr=False)


def prepare(root: Path) -> ScenarioDatabase:
    """Load the explicitly selected local database credentials."""
    root = root.resolve()
    return ScenarioDatabase(root, read_environment(root / "admin.env")["ADMIN_DATABASE_URL"])


def apply(
    database: ScenarioDatabase,
    names: list[str] | None = None,
    *,
    file: Path | None = None,
) -> list[dict]:
    """Apply trusted SQL in one transaction; files must not manage transactions."""
    if file is not None and names:
        raise ValueError("Choose scenario names or --file, not both")
    selected = list(SCENARIOS) if names is None else names
    if file is not None:
        paths = [file]
    else:
        unknown = set(selected) - SCENARIOS.keys()
        if unknown:
            raise ValueError("Unknown scenarios: " + ", ".join(sorted(unknown)))
        paths = [SCENARIO_DIR / (name + ".sql") for name in dict.fromkeys(selected)]
    sources = [path.read_text(encoding="utf-8-sig") for path in paths]
    with psycopg.connect(database.admin_dsn, row_factory=dict_row, connect_timeout=10) as conn:
        marker = conn.execute("SELECT to_regclass('dl_test.marker') AS marker").fetchone()
        if marker["marker"] is None:
            raise RuntimeError("Refusing scenario writes: test database marker is missing")
        if not conn.execute(
            "SELECT EXISTS (SELECT FROM dl_test.marker WHERE singleton) AS marked"
        ).fetchone()["marked"]:
            raise RuntimeError("Refusing scenario writes: database is not marked for testing")
        conn.execute("SET LOCAL ROLE dl_owner")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS dl_test.scenario ("
            "slug text PRIMARY KEY, "
            "league_id integer NOT NULL UNIQUE REFERENCES dl.league(id))"
        )
        for source in sources:
            conn.execute(source, prepare=False)
        return conn.execute(
            "SELECT f.slug AS scenario, l.id AS league_id, l.name, s.status "
            "FROM dl_test.scenario f JOIN dl.league l ON l.id = f.league_id "
            "JOIN dl.draft_state s ON s.league_id = l.id ORDER BY f.slug"
        ).fetchall()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="Managed disposable test cluster directory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Show the editable built-in SQL scenarios")
    command = commands.add_parser("apply", help="Replace selected fixtures (default: all)")
    command.add_argument("names", nargs="*", help="Scenario names, or all")
    command.add_argument("--file", type=Path, help="Run your own SQL file in one transaction")
    args = parser.parse_args()
    if args.command == "list":
        for name, description in SCENARIOS.items():
            print(f"{name:10} {description}")
        return
    if args.root is None:
        parser.error("apply requires --root for a managed test cluster")
    if args.file and args.names:
        parser.error("Choose scenario names or --file, not both")
    names = None if args.names in ([], ["all"]) else args.names
    try:
        rows = apply(prepare(args.root), names, file=args.file)
    except (psycopg.Error, OSError, RuntimeError, ValueError) as exc:
        detail = (
            f"database operation failed (SQLSTATE {exc.sqlstate or 'connection error'})"
            if isinstance(exc, psycopg.Error)
            else str(exc)
        )
        print(f"Scenario load failed: {detail}", file=sys.stderr)
        raise SystemExit(1) from None
    print("SQL committed. Refresh the frontend league list to explore the new fixtures.")
    for row in rows:
        print(f"{row['scenario']:10} league {row['league_id']}: {row['name']} ({row['status']})")


if __name__ == "__main__":
    main()
