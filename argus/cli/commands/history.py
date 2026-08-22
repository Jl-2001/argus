"""`argus history <application> [--since DURATION]` -- chronological
health transitions for one application's whole entity tree.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from argus.cli import queries
from argus.cli.durations import InvalidDurationError, parse_duration
from argus.cli.formatting import iso, render_table
from argus.store.repository import Repository

COMMAND = "history"

DEFAULT_SINCE = "24h"


def _since_type(text: str) -> timedelta:
    try:
        return parse_duration(text)
    except InvalidDurationError as exc:
        # argparse converts this into a clean usage error (exit 2) on its
        # own -- no manual handling needed here.
        raise argparse.ArgumentTypeError(str(exc)) from exc


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(COMMAND, help="Chronological health transitions for one application")
    parser.add_argument("application", help="Application name or key (case-insensitive)")
    parser.add_argument(
        "--since",
        type=_since_type,
        default=parse_duration(DEFAULT_SINCE),
        metavar="DURATION",
        help=f"How far back to look, e.g. 30m/6h/24h/7d (default: {DEFAULT_SINCE})",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace, repository: Repository, now: datetime) -> int:
    since = now - args.since
    entries = queries.list_history(repository, name_or_key=args.application, since=since)

    if entries is None:
        print(f"Application {args.application!r} not found.", file=sys.stderr)
        suggestion = queries.suggest_application_name(repository, args.application)
        if suggestion is not None:
            print(f"Did you mean {suggestion!r}?", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "application": args.application,
            "since": iso(since),
            "transitions": [
                {
                    "occurred_at": iso(entry.occurred_at),
                    "scope": entry.scope,
                    "label": entry.label,
                    "from_status": entry.from_status.value if entry.from_status is not None else None,
                    "to_status": entry.to_status.value,
                }
                for entry in entries
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0

    if not entries:
        print("No transitions recorded in this window.")
        return 0

    rows = [
        [
            entry.occurred_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + "Z",
            entry.label,
            f"{entry.from_status.value if entry.from_status is not None else 'NULL'} -> {entry.to_status.value}",
        ]
        for entry in entries
    ]
    print(render_table(["", "", ""], rows, show_header=False))
    return 0
