"""Load editable SQL scenarios into a marked, disposable local test database."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

if __package__:
    from .testdb import PROJECT, load_marker
else:
    from testdb import PROJECT, load_marker

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
    """Validate a managed cluster and load its credentials without displaying them."""
    marker = load_marker(root)
    root = root.resolve()
    path = root / "admin.env"
    if path.is_symlink():
        raise RuntimeError("Refusing a symbolic-link administrator environment file")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    dsn = values.get("ADMIN_DATABASE_URL", "")
    try:
        connection = conninfo_to_dict(dsn)
    except psycopg.Error:
        raise RuntimeError("Invalid administrator connection in the marked cluster") from None
    if (
        connection.get("host") != "127.0.0.1"
        or connection.get("port") != str(marker["port"])
        or connection.get("dbname") != marker["database"]
        or connection.get("user") != "postgres"
    ):
        raise RuntimeError("Administrator connection does not match the marked local cluster")
    return ScenarioDatabase(root, dsn)


def sql_statements(source: str) -> list[tuple[int, int, list[str]]]:
    """Find top-level statement words, ignoring PostgreSQL quotes and comments."""
    statements, words = [], []
    position, start, length = 0, None, len(source)
    while position < length:
        char = source[position]
        if char.isspace():
            position += 1
            continue
        if source.startswith("--", position):
            newline = source.find("\n", position + 2)
            position = length if newline < 0 else newline + 1
            continue
        if source.startswith("/*", position):
            position, depth = position + 2, 1
            while position < length and depth:
                if source.startswith("/*", position):
                    depth, position = depth + 1, position + 2
                elif source.startswith("*/", position):
                    depth, position = depth - 1, position + 2
                else:
                    position += 1
            if depth:
                raise ValueError("Unterminated SQL comment")
            continue
        if start is None:
            start = position
        if char in "'\"":
            escape = (char == "'" and position > 0 and source[position - 1] in "eE"
                      and (position < 2 or not source[position - 2].isalnum()))
            position += 1
            while position < length:
                if escape and source[position] == "\\":
                    position += 2
                elif source[position] == char:
                    position += 1
                    if position < length and source[position] == char:
                        position += 1
                    else:
                        break
                else:
                    position += 1
            else:
                raise ValueError("Unterminated SQL quoted value")
            continue
        dollar = re.match(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$", source[position:])
        if dollar:
            delimiter = dollar.group()
            close = source.find(delimiter, position + len(delimiter))
            if close < 0:
                raise ValueError("Unterminated SQL dollar-quoted body")
            position = close + len(delimiter)
            continue
        word = re.match(r"[A-Za-z_][A-Za-z_0-9$]*", source[position:])
        if word:
            words.append(word.group().upper())
            position += len(word.group())
            continue
        position += 1
        if char == ";":
            if words:
                statements.append((start, position, words))
            start, words = None, []
    if words:
        statements.append((start, length, words))
    return statements


def transaction_body(source: str) -> str:
    """Strip optional outer BEGIN/COMMIT; prohibit anything that could commit early."""
    statements = sql_statements(source)
    controls = [index for index, (_, _, words) in enumerate(statements)
                if words[0] in {"BEGIN", "COMMIT", "END", "ROLLBACK", "ABORT", "START"}
                or words[:2] == ["PREPARE", "TRANSACTION"]]
    if not controls:
        return source
    if (len(controls) != 2 or controls != [0, len(statements) - 1]
            or statements[0][2] != ["BEGIN"] or statements[-1][2] != ["COMMIT"]):
        raise ValueError("SQL files may contain only one outer BEGIN/COMMIT transaction")
    first, last = statements[0], statements[-1]
    return source[:first[0]] + source[first[1]:last[0]] + source[last[1]:]


def apply(
    database: ScenarioDatabase,
    names: list[str] | None = None,
    *,
    file: Path | None = None,
) -> list[dict]:
    """Apply raw SQL atomically; only fixture SQL decides which rows to replace."""
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
    sources = [transaction_body(path.read_text(encoding="utf-8-sig")) for path in paths]
    with psycopg.connect(database.admin_dsn, row_factory=dict_row, connect_timeout=10) as conn:
        marker = conn.execute("SELECT to_regclass('dl_test.marker') AS marker").fetchone()
        if marker["marker"] is None:
            raise RuntimeError("Refusing scenario writes: test database marker is missing")
        if not conn.execute(
            "SELECT EXISTS (SELECT FROM dl_test.marker WHERE singleton) AS marked"
        ).fetchone()["marked"]:
            raise RuntimeError("Refusing scenario writes: database is not marked for testing")
        conn.execute("SET LOCAL ROLE dl_owner")
        for source in sources:
            conn.execute(source, prepare=False)
        registry = conn.execute("SELECT to_regclass('dl_test.scenario') AS registry").fetchone()
        if registry["registry"] is None:
            return []
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
            if isinstance(exc, psycopg.Error) else str(exc)
        )
        print(f"Scenario load failed: {detail}", file=sys.stderr)
        raise SystemExit(1) from None
    print("SQL committed. Refresh the frontend league list to explore the new fixtures.")
    for row in rows:
        print(f"{row['scenario']:10} league {row['league_id']}: {row['name']} ({row['status']})")


if __name__ == "__main__":
    main()
