"""Process entrypoint: ``python -m argus.run_collector``.

Pure composition and bootstrap -- opens the database, migrates its
schema if needed, constructs the Docker client, repository, and
collector loop, then runs forever until interrupted. No collector
business logic lives here; see ``argus.collector.loop`` for that.
"""

from __future__ import annotations

import logging
import os
import sys

from argus.collector.loop import DEFAULT_COLLECTOR_CONFIG, CollectorLoop
from argus.collectors.docker_client import DockerClient, DockerUnavailableError
from argus.store.database import DatabaseOpenError, SchemaError, default_database_path, open_database
from argus.store.repository import Repository

logger = logging.getLogger("argus.run_collector")

#: Milestone 16 -- lets a control-plane operator give their own machine
#: a real display name (e.g. "MacBook") in `argus agents`/`GET
#: /api/v1/hosts`, the same way a remote agent's `ARGUS_HOST_NAME` does
#: (see `argus.agent.config`). Purely cosmetic -- `LOCAL_HOST_KEY`
#: ("local") is never configurable, since it's the one thing this
#: whole migration's backward compatibility depends on staying fixed.
_DEFAULT_LOCAL_HOST_NAME = "Local Host"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    db_path = default_database_path()

    # A database Argus can't open/initialize/migrate is fatal at startup --
    # there is nowhere trustworthy to persist state, so there is no
    # reasonable degraded mode to fall back to. This is deliberately
    # different from a *transient* persistence failure during a running
    # tick, which argus.collector.loop already treats as a recoverable,
    # backed-off failure rather than a reason to stop the process.
    try:
        connection = open_database(db_path)
    except (DatabaseOpenError, SchemaError) as exc:
        logger.error("cannot start Argus: database unavailable at %s: %s", db_path, exc)
        return 1

    # Likewise, if Docker itself can't be reached at all at the moment
    # Argus starts, that's treated the same way -- fail fast with a clear
    # message. (Docker going away *while already running* is the case the
    # collector loop's backoff exists for, and is fully recoverable; that
    # is a different scenario from "was never reachable in the first
    # place".)
    try:
        client = DockerClient()
    except DockerUnavailableError as exc:
        logger.error("cannot start Argus: Docker unavailable: %s", exc)
        connection.close()
        return 1

    repository = Repository(connection)
    host_display_name = os.environ.get("ARGUS_HOST_NAME", _DEFAULT_LOCAL_HOST_NAME)
    loop = CollectorLoop(
        client=client,
        repository=repository,
        config=DEFAULT_COLLECTOR_CONFIG,
        host_display_name=host_display_name,
    )

    logger.info(
        "Argus collector starting (db=%s, poll_interval=%ss)",
        db_path,
        DEFAULT_COLLECTOR_CONFIG.poll_interval,
    )
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        logger.info("Argus collector stopping (interrupted)")
    finally:
        connection.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
