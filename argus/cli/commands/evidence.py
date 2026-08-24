"""`argus evidence <application> [--since DURATION | --incident ID]` --
deterministic evidence collected around an application's containers.

Read-only, same as every other command here: everything comes from
already-persisted `log_signals`/`incident_evidence` rows -- this module
never talks to Docker, never classifies a log line, and never claims a
signal *caused* anything (see `argus.evidence.association`'s own
module docstring for why that distinction is structural, not just a
convention).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from argus.cli import queries
from argus.cli.durations import InvalidDurationError, parse_duration
from argus.cli.formatting import iso, render_table
from argus.store.repository import Repository

COMMAND = "evidence"

DEFAULT_SINCE = "24h"


def _since_type(text: str):
    try:
        return parse_duration(text)
    except InvalidDurationError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        COMMAND, help="Deterministic evidence collected around one application's containers"
    )
    parser.add_argument("application", help="Application name or key (case-insensitive)")
    parser.add_argument(
        "--since", type=_since_type, default=None, metavar="DURATION",
        help=f"How far back to look, e.g. 30m/6h/24h/7d (default: {DEFAULT_SINCE}; ignored with --incident)",
    )
    parser.add_argument(
        "--incident", type=int, default=None, metavar="ID",
        help="Show only evidence linked to this incident (a detailed per-signal view, not the summary table)",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace, repository: Repository, now: datetime) -> int:
    if args.incident is not None:
        return _run_for_incident(args, repository, now)
    return _run_for_application(args, repository, now)


def _run_for_application(args: argparse.Namespace, repository: Repository, now: datetime) -> int:
    since_delta = args.since if args.since is not None else parse_duration(DEFAULT_SINCE)
    since = now - since_delta

    views = queries.list_evidence_for_application(repository, name_or_key=args.application, since=since)
    if views is None:
        print(f"Application {args.application!r} not found.", file=sys.stderr)
        suggestion = queries.suggest_application_name(repository, args.application)
        if suggestion is not None:
            print(f"Did you mean {suggestion!r}?", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"application": args.application, "since": iso(since), "evidence": _views_to_json(views)}, indent=2))
        return 0

    if not views:
        print("No evidence recorded in this window.")
        return 0

    rows = [
        [
            entry.first_seen_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + "Z",
            entry.severity.value,
            entry.category.value,
            str(entry.count),
            entry.source_label,
        ]
        for entry in views
    ]
    print(render_table(["TIME", "SEVERITY", "CATEGORY", "COUNT", "SOURCE"], rows))
    return 0


def _run_for_incident(args: argparse.Namespace, repository: Repository, now: datetime) -> int:
    # Resolve the application first, purely so a mismatched --incident
    # (one that doesn't belong to this application) produces a clear
    # error instead of silently showing another application's evidence.
    application_key = queries.find_application_key(repository, args.application)
    if application_key is None:
        print(f"Application {args.application!r} not found.", file=sys.stderr)
        suggestion = queries.suggest_application_name(repository, args.application)
        if suggestion is not None:
            print(f"Did you mean {suggestion!r}?", file=sys.stderr)
        return 1

    incident = repository.get_incident_by_id(args.incident)
    if incident is None:
        print(f"Incident #{args.incident} not found.", file=sys.stderr)
        return 1

    application_record = repository.get_application(application_key)
    if incident.scope_id != application_record.id:
        print(f"Incident #{args.incident} does not belong to application {args.application!r}.", file=sys.stderr)
        return 1

    views = queries.list_evidence_for_incident(repository, incident_id=args.incident)
    assert views is not None  # already confirmed to exist above

    if args.json:
        print(json.dumps({"incident": args.incident, "evidence": _views_to_json(views)}, indent=2))
        return 0

    if not views:
        print(f"No evidence linked to incident #{args.incident}.")
        return 0

    print(f"Evidence for incident #{args.incident}\n")
    for index, entry in enumerate(views, start=1):
        print(f"[{index}] {entry.category.value} · {entry.severity.value} · count={entry.count}")
        print(f"    First seen   {iso(entry.first_seen_at)}")
        print(f"    Last seen    {iso(entry.last_seen_at)}")
        print(f"    Source       {entry.source_label}")
        print(f"    Sample       {entry.sample}")
        print()
    return 0


def _views_to_json(views) -> list[dict]:
    return [
        {
            "category": entry.category.value,
            "severity": entry.severity.value,
            "count": entry.count,
            "first_seen_at": iso(entry.first_seen_at),
            "last_seen_at": iso(entry.last_seen_at),
            "sample": entry.sample,
            "source": entry.source_label,
            "source_type": entry.source_type,
        }
        for entry in views
    ]
