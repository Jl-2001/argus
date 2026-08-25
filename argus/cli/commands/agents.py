"""`argus agents` / `argus agents inspect <host>` / `argus agents add
<host-key>` -- Milestone 16.

`agents`/`agents inspect` are ordinary read commands, built from the
exact same `argus.cli.queries.list_host_views`/`get_host_detail` the
API's own `argus.api.routes.hosts` uses -- one read model, two
transports, same discipline as every other command in this package.

`agents add` is the one deliberate exception: an *administrative*
write, called directly against `Repository.create_agent_host` (never
through a read-model function) -- see this module's own docstring on
`_add` for why registration is explicitly out-of-band from every other
command here.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime

from argus.cli import queries
from argus.cli.formatting import EM_DASH, iso, relative_time, render_table
from argus.security import generate_token, hash_token
from argus.store.repository import Repository

COMMAND = "agents"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(COMMAND, help="List/inspect/register monitored hosts and their agents")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead")
    agent_subparsers = parser.add_subparsers(dest="agents_subcommand")

    inspect_parser = agent_subparsers.add_parser("inspect", help="Detail for one host")
    inspect_parser.add_argument("host_key", help="The host's host_key, e.g. dell-latitude-5400")
    inspect_parser.set_defaults(func=_inspect)

    add_host_parser = agent_subparsers.add_parser(
        "add", help="Register a new remote agent host and generate its credential"
    )
    add_host_parser.add_argument("host_key", help="A stable, unique identifier, e.g. dell-latitude-5400")
    add_host_parser.add_argument("--name", required=True, help="Human-facing display name, e.g. 'Ubuntu Dell'")
    add_host_parser.set_defaults(func=_add)

    parser.set_defaults(func=_list)


def _list(args: argparse.Namespace, repository: Repository, now: datetime) -> int:
    hosts = queries.list_host_views(repository, now=now)

    if args.json:
        print(json.dumps({"hosts": [_host_to_json(h) for h in hosts]}, indent=2))
        return 0

    if not hosts:
        print("No hosts registered yet.")
        return 0

    rows = [
        [h.display_name, h.status.value, relative_time(now, h.last_seen_at), str(h.application_count)]
        for h in hosts
    ]
    print(render_table(["HOST", "STATUS", "LAST SEEN", "APPS"], rows))
    return 0


def _inspect(args: argparse.Namespace, repository: Repository, now: datetime) -> int:
    detail = queries.get_host_detail(repository, now=now, host_key=args.host_key)
    if detail is None:
        print(f"No host found with host_key {args.host_key!r}.")
        return 1

    if args.json:
        print(json.dumps(
            {
                **_host_to_json(detail.summary),
                "first_seen_at": iso(detail.first_seen_at),
                "applications": [
                    {"key": a.key, "name": a.name, "status": a.status.value}
                    for a in detail.applications
                ],
            },
            indent=2,
        ))
        return 0

    summary = detail.summary
    print(f"{summary.display_name} ({summary.host_key})")
    print(f"  Kind:          {summary.kind}")
    print(f"  Status:        {summary.status.value}")
    print(f"  Last seen:     {relative_time(now, summary.last_seen_at)}")
    print(f"  Agent version: {summary.agent_version or EM_DASH}")
    print(f"  Applications:  {summary.application_count}")
    if detail.applications:
        print("")
        rows = [[a.name, a.status.value] for a in detail.applications]
        print(render_table(["APPLICATION", "STATUS"], rows))
    return 0


def _add(args: argparse.Namespace, repository: Repository, now: datetime) -> int:
    """Administrative registration -- generates a fresh, high-entropy
    token (`argus.security.generate_token`), persists only its hash
    (`Repository.create_agent_host`), and prints the plaintext token
    exactly once, right here, right now. This is the one and only place
    in all of Argus a plaintext agent token is ever displayed -- it is
    never logged, never re-derivable, and never returned by any other
    command or API route afterward (see `argus.store.repository
    .HostRecord`'s own docstring).
    """

    token = generate_token()
    # A random, opaque credential identity, distinct from `host_key` --
    # see schema.sql's own comment on `hosts.agent_id` for why
    # authentication is checked against this, not the human-chosen
    # host_key. Generated the same high-entropy way as the token
    # itself; there is no reason for it to be guessable either.
    agent_id = f"agent-{generate_token()}"

    try:
        repository.create_agent_host(
            host_key=args.host_key, agent_id=agent_id, display_name=args.name,
            token_hash=hash_token(token), now=now,
        )
    except Exception as exc:  # noqa: BLE001 -- Repository.create_agent_host's own PersistenceError, reported plainly
        print(f"Could not register host {args.host_key!r}: {exc}")
        return 1

    print(f"Host {args.host_key!r} ({args.name}) registered.")
    print("")
    print("Set the following on the remote machine (argus-agent), and nowhere else:")
    print("")
    print(f"  ARGUS_AGENT_ID={agent_id}")
    print(f"  ARGUS_AGENT_TOKEN={token}")
    print(f"  ARGUS_HOST_KEY={args.host_key}")
    print(f"  ARGUS_HOST_NAME=\"{args.name}\"")
    print("")
    print(
        "This token is shown once and is not recoverable -- only its hash is stored. "
        "If it is lost, register a new host (or extend this command with a rotate "
        "operation later)."
    )
    return 0


def _host_to_json(view: "queries.HostView") -> dict:
    return {
        "host_key": view.host_key,
        "display_name": view.display_name,
        "kind": view.kind,
        "status": view.status.value,
        "last_seen_at": iso(view.last_seen_at),
        "agent_version": view.agent_version,
        "application_count": view.application_count,
    }
