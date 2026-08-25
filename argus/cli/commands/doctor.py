"""`argus doctor` -- live prerequisite diagnostics.

Unlike every other command module here, this one does not receive an
already-open `Repository` from `main.py`: the database's own
openability is itself one of the things being diagnosed, so `main.py`
dispatches to `run()` before attempting to open anything. See
`main.py`'s docstring for why.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from argus.cli.formatting import render_table
from argus.doctor.checks import CheckStatus, DoctorResult, run_checks

COMMAND = "doctor"

#: Machine name -> human label, in the same fixed order checks run in.
_DISPLAY_NAMES: dict[str, str] = {
    "configuration": "Configuration",
    "database": "Database",
    "docker_connection": "Docker connection",
    "docker_read_access": "Docker read access",
    "collector_heartbeat": "Collector heartbeat",
    "remote_agents": "Remote agents",
    "clock": "Clock",
}


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        COMMAND, help="Diagnose whether Argus currently has what it needs to monitor"
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead")
    # No `set_defaults(func=...)`: doctor is dispatched specially by
    # main.py, before the database is opened -- see main.py.


def run(args: argparse.Namespace, *, db_path: Path, now: datetime) -> int:
    result = run_checks(db_path=db_path, now=now)

    if args.json:
        print(json.dumps(_to_json(result), indent=2))
    else:
        print(_render_human(result))

    return 0 if result.operational else 1


def _to_json(result: DoctorResult) -> dict:
    return {
        "operational": result.operational,
        "checks": [
            {"name": check.name, "status": check.status.value, "message": check.message}
            for check in result.checks
        ],
    }


def _render_human(result: DoctorResult) -> str:
    lines = ["ARGUS DOCTOR", ""]
    rows = [[_DISPLAY_NAMES[check.name], check.status.value] for check in result.checks]
    lines.append(render_table(["", ""], rows, show_header=False))

    problems = [
        check for check in result.checks if check.status in (CheckStatus.FAIL, CheckStatus.WARN)
    ]
    if problems:
        lines.append("")
        lines.append("Problems detected:")
        for check in problems:
            lines.append("")
            lines.append(_DISPLAY_NAMES[check.name])
            lines.append(f"  {check.message}")

    lines.append("")
    if any(check.status is CheckStatus.FAIL for check in result.checks):
        lines.append("Argus is not fully operational.")
    elif any(check.status is CheckStatus.WARN for check in result.checks):
        lines.append("Argus is operational, but degraded.")
    else:
        lines.append("Argus is operational.")

    return "\n".join(lines)
