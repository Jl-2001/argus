"""`argus status` -- one-screen overview of the collector and every
discovered application. Everything comes from the database; there is
no live Docker call anywhere in this module.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime

from argus.cli import queries
from argus.cli.formatting import iso, relative_time, render_kv, render_table
from argus.store.repository import Repository

COMMAND = "status"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(COMMAND, help="One-screen overview of Argus and its applications")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace, repository: Repository, now: datetime) -> int:
    collector_status = queries.get_collector_status(repository, now=now)
    applications = queries.list_application_summaries(repository, now=now)
    open_incidents = len(queries.list_incidents(repository, open_only=True))

    if args.json:
        payload = {
            "collector": {
                "status": collector_status.classification,
                "last_tick_at": iso(collector_status.last_tick_at),
                "last_success_at": iso(collector_status.last_success_at),
                "consecutive_failures": collector_status.consecutive_failures,
                "last_error": collector_status.last_error,
            },
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
            ],
            "open_incidents": open_incidents,
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(_render_human(collector_status, applications, open_incidents, now))
    return 0


def _render_human(collector_status, applications, open_incidents: int, now: datetime) -> str:
    lines = ["ARGUS", "", "Collector"]

    kv_pairs = [("Status", collector_status.classification)]
    if collector_status.classification == "NEVER_RUN":
        lines.append(render_kv(kv_pairs))
        lines.append("")
        lines.append("Argus has never completed a collection tick.")
    else:
        kv_pairs.append(("Last tick", relative_time(now, collector_status.last_tick_at)))
        kv_pairs.append(("Last success", relative_time(now, collector_status.last_success_at)))
        kv_pairs.append(("Failures", str(collector_status.consecutive_failures)))
        if collector_status.last_error:
            kv_pairs.append(("Last error", collector_status.last_error))
        lines.append(render_kv(kv_pairs))

    lines.append("")
    if not applications:
        lines.append("No applications discovered yet.")
        return "\n".join(lines)

    lines.append("Applications")
    lines.append("")
    rows = [
        [app.name, app.status.value, str(app.service_count), str(app.container_count)]
        for app in applications
    ]
    lines.append(render_table(["NAME", "STATUS", "SERVICES", "CONTAINERS"], rows))
    lines.append("")

    app_word = "application" if len(applications) == 1 else "applications"
    incident_word = "incident" if open_incidents == 1 else "incidents"
    lines.append(f"{len(applications)} {app_word} · {open_incidents} open {incident_word}")
    return "\n".join(lines)
