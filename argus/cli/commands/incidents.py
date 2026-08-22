"""`argus incidents [--open]` -- incident history, newest opened first."""

from __future__ import annotations

import argparse
import json
from datetime import datetime

from argus.cli import queries
from argus.cli.formatting import EM_DASH, iso, relative_time, render_table
from argus.store.repository import Repository

COMMAND = "incidents"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(COMMAND, help="Show incident history")
    parser.add_argument("--open", action="store_true", dest="open_only", help="Only open incidents")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace, repository: Repository, now: datetime) -> int:
    incidents = queries.list_incidents(repository, open_only=args.open_only)

    if args.json:
        payload = {
            "incidents": [
                {
                    "id": incident.id,
                    "application": incident.application_name,
                    "application_key": incident.application_key,
                    "status": incident.status,
                    "opened_at": iso(incident.opened_at),
                    "closed_at": iso(incident.closed_at),
                    "opening_status": incident.opening_status.value,
                    "worst_status": incident.worst_status.value,
                }
                for incident in incidents
            ]
        }
        print(json.dumps(payload, indent=2))
        return 0

    if not incidents:
        print("No incidents recorded." if not args.open_only else "No open incidents.")
        return 0

    rows = [
        [
            str(incident.id),
            incident.application_name,
            incident.status,
            relative_time(now, incident.opened_at),
            relative_time(now, incident.closed_at) if incident.closed_at is not None else EM_DASH,
            incident.worst_status.value,
        ]
        for incident in incidents
    ]
    print(render_table(["ID", "APPLICATION", "STATUS", "OPENED", "CLOSED", "WORST"], rows))
    return 0
