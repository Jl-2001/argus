"""``argus`` -- the console entry point (see `[project.scripts]` in
pyproject.toml). Pure composition: parses arguments, resolves and opens
the database, reads `now` once, dispatches to the chosen command, and
closes the database. No query logic, no formatting, no business rules
live here.

``argus doctor`` is the one deliberate exception to "open the database,
then dispatch": whether the database can be opened at all is itself
one of the things doctor diagnoses, so it must run even when the
shared ``open_database()`` step below would otherwise fail the whole
command with "database unavailable" before doctor ever got a chance to
say so itself. It is dispatched before that step, given only the
resolved path (via ``default_database_path(..., create_parent=False)``,
so merely running ``argus doctor`` against a nonexistent database never
has the side effect of creating one) -- every other command's existing
behavior is completely unchanged.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Optional, Sequence

from argus.cli.commands import apps, doctor, history, incidents, inspect as inspect_cmd, status
from argus.store.database import DatabaseOpenError, SchemaError, default_database_path, open_database
from argus.store.repository import Repository

__all__ = ["build_parser", "main"]

_COMMAND_MODULES = (status, apps, inspect_cmd, incidents, history, doctor)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="argus",
        description="Argus: a deterministic infrastructure monitoring substrate. "
        "Reads already-persisted state -- it never talks to Docker directly.",
    )
    parser.add_argument(
        "--database",
        default=None,
        metavar="PATH",
        help="Path to the Argus SQLite database "
        "(overrides ARGUS_DB_PATH; default: ./data/argus.db)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for module in _COMMAND_MODULES:
        module.add_parser(subparsers)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)

    if args.command == doctor.COMMAND:
        db_path = default_database_path(args.database, create_parent=False)
        return doctor.run(args, db_path=db_path, now=now)

    db_path = default_database_path(args.database)

    try:
        connection = open_database(db_path)
    except (DatabaseOpenError, SchemaError) as exc:
        print(f"Argus database unavailable: {exc}", file=sys.stderr)
        return 1

    try:
        repository = Repository(connection)
        return args.func(args, repository, now)
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
