"""`argus bundle <incident_id> [--json] [--full]` -- preview the exact
evidence bundle a future AI reasoning layer would receive for one
incident.

Read-only, like every other command here: `assemble_evidence_bundle`
never touches Docker and never writes to the database. This command is
deliberately the *only* thing standing between "an incident exists" and
"a bundle gets built" -- there is no AI call anywhere in this module.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from argus.cli.formatting import render_table
from argus.evidence.assembler import DEFAULT_ASSEMBLER_CONFIG, IncidentNotFoundError, assemble_evidence_bundle
from argus.store.repository import Repository

COMMAND = "bundle"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        COMMAND, help="Preview the evidence bundle a future AI layer would receive for one incident"
    )
    parser.add_argument("incident_id", type=int, help="Incident id (see `argus incidents`)")
    parser.add_argument("--json", action="store_true", help="Print the full, machine-readable bundle as JSON")
    parser.add_argument(
        "--full", action="store_true",
        help="Also print the full timeline in human mode (default: summary counts only)",
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace, repository: Repository, now: datetime) -> int:
    try:
        bundle = assemble_evidence_bundle(repository, args.incident_id, now=now, config=DEFAULT_ASSEMBLER_CONFIG)
    except IncidentNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.json:
        print(bundle.to_json())
        return 0

    print(_render_summary(bundle))
    if args.full:
        print()
        print(_render_timeline(bundle))
    return 0


def _render_summary(bundle) -> str:
    lines = [
        f"Incident #{bundle.incident.incident_id}",
        f"Application {bundle.application.name}",
        f"Window {bundle.window.start.isoformat()} -> {bundle.window.end.isoformat()}"
        + (" (open)" if bundle.window.incident_open else ""),
        "",
        f"Signals       {len(bundle.signals)}",
        f"Transitions   {len(bundle.transitions)}",
        f"Observations  {len(bundle.observations)}",
        f"Truncated     {'yes' if bundle.metadata.truncated else 'no'}",
        f"Evidence subsystem  {bundle.metadata.evidence_subsystem_status}",
        f"Fingerprint   {bundle.metadata.fingerprint}",
    ]
    omitted = bundle.metadata.omitted_counts
    if any(count > 0 for count in omitted.values()):
        lines.append(
            f"Omitted       signals={omitted.get('signals', 0)} "
            f"transitions={omitted.get('transitions', 0)} observations={omitted.get('observations', 0)}"
        )
    return "\n".join(lines)


def _render_timeline(bundle) -> str:
    rows = [
        [
            entry.timestamp.strftime("%Y-%m-%d %H:%M:%S") + "Z",
            entry.entry_type,
            entry.entity,
            entry.facts,
            entry.reference,
        ]
        for entry in bundle.timeline
    ]
    if not rows:
        return "No timeline entries in this window."
    return render_table(["TIME", "TYPE", "ENTITY", "FACTS", "REFERENCE"], rows)
