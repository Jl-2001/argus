"""`argus apps` -- list all known applications."""

from __future__ import annotations

import argparse
import json
from datetime import datetime

from argus.cli import queries
from argus.cli.formatting import iso, relative_time, render_table
from argus.store.repository import Repository

COMMAND = "apps"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(COMMAND, help="List all known applications")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace, repository: Repository, now: datetime) -> int:
    applications = queries.list_application_summaries(repository, now=now)

    if args.json:
        payload = {
            "applications": [
                {
                    "key": app.key,
                    "name": app.name,
                    "status": app.status.value,
                    "services": app.service_count,
                    "containers": app.container_count,
                    "last_seen_at": iso(app.last_seen_at),
                }
                for app in applications
            ]
        }
        print(json.dumps(payload, indent=2))
        return 0

    if not applications:
        print("No applications discovered yet.")
        return 0

    # Deterministic ordering: application name ascending (already the order
    # Repository.list_applications_with_counts returns, sorted by key) --
    # a stable table a human can compare run to run, rather than reordering
    # by severity.
    rows = [
        [
            app.name,
            app.status.value,
            str(app.service_count),
            str(app.container_count),
            relative_time(now, app.last_seen_at),
        ]
        for app in applications
    ]
    print(render_table(["NAME", "STATUS", "SERVICES", "CONTAINERS", "LAST SEEN"], rows))
    return 0
