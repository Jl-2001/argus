"""`argus inspect <application>` -- detailed current view of one application."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from argus.cli import queries
from argus.cli.formatting import EM_DASH, iso, relative_time, render_kv, render_table
from argus.store.repository import Repository

COMMAND = "inspect"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(COMMAND, help="Detailed current view of one application")
    parser.add_argument("application", help="Application name or key (case-insensitive)")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace, repository: Repository, now: datetime) -> int:
    detail = queries.get_application_detail(repository, now=now, name_or_key=args.application)

    if detail is None:
        print(f"Application {args.application!r} not found.", file=sys.stderr)
        suggestion = queries.suggest_application_name(repository, args.application)
        if suggestion is not None:
            print(f"Did you mean {suggestion!r}?", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(_to_json(detail), indent=2))
        return 0

    print(_render_human(detail, now))
    return 0


def _to_json(detail) -> dict:
    return {
        "key": detail.key,
        "name": detail.name,
        "status": detail.status.value,
        "last_seen_at": iso(detail.last_seen_at),
        "services": [
            {
                "compose_service": service.compose_service,
                "name": service.display_name,
                "status": service.status.value,
                "container": (
                    {
                        "name": service.container.name,
                        "docker_state": service.container.docker_state,
                        "docker_health": service.container.docker_health,
                        "restart_count": service.container.restart_count,
                        "ports": [
                            {
                                "container_port": port.container_port,
                                "protocol": port.protocol,
                                "host_binding": port.host_binding,
                            }
                            for port in service.container.ports
                        ],
                    }
                    if service.container is not None
                    else None
                ),
            }
            for service in detail.services
        ],
        "open_incident": (
            {
                "id": detail.open_incident.id,
                "status": detail.open_incident.status,
                "opened_at": iso(detail.open_incident.opened_at),
                "closed_at": iso(detail.open_incident.closed_at),
                "opening_status": detail.open_incident.opening_status.value,
                "worst_status": detail.open_incident.worst_status.value,
            }
            if detail.open_incident is not None
            else None
        ),
    }


def _render_human(detail, now: datetime) -> str:
    lines = [f"{detail.name} · {detail.status.value}", ""]

    service_rows = []
    port_rows = []
    for service in detail.services:
        container = service.container
        service_rows.append(
            [
                service.display_name,
                service.status.value,
                container.name if container is not None else EM_DASH,
                container.docker_state if container is not None else EM_DASH,
                (container.docker_health or EM_DASH) if container is not None else EM_DASH,
                str(container.restart_count) if container is not None else EM_DASH,
            ]
        )
        if container is not None:
            for port in container.ports:
                port_rows.append(
                    [
                        service.display_name,
                        f"{port.container_port}/{port.protocol}",
                        port.host_binding if port.host_binding is not None else "internal",
                    ]
                )

    lines.append(
        render_table(
            ["SERVICE", "STATUS", "CONTAINER", "DOCKER STATE", "DOCKER HEALTH", "RESTARTS"], service_rows
        )
    )

    if port_rows:
        lines.append("")
        lines.append("Ports")
        lines.append(render_table(["SERVICE", "PORT", "HOST BINDING"], port_rows))

    if detail.open_incident is not None:
        incident = detail.open_incident
        lines.append("")
        lines.append(f"Open incident #{incident.id}")
        lines.append(
            render_kv(
                [
                    ("Opened", relative_time(now, incident.opened_at)),
                    ("Opening state", incident.opening_status.value),
                    ("Worst state", incident.worst_status.value),
                ]
            )
        )

    return "\n".join(lines)
