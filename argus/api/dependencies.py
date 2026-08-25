"""FastAPI `Depends()` providers: a request-scoped `Repository` and a
request-scoped `now`. Every route function receives both instead of
opening a connection or reading the clock itself -- the same
open-once-per-invocation, read-the-clock-once discipline
`argus.cli.main` already uses, applied per-request instead of per-CLI-
invocation.

A fresh SQLite connection is opened and closed for every single
request rather than shared across requests. Deliberate: `sqlite3`
connections are not safe to share across threads, and a sync FastAPI
route can run in a worker thread; a temp-DB test may run many requests
back to back. This is local/homelab scale (see the milestone's own
"Performance" note) -- the per-request `open_database` cost (a few
idempotent `CREATE TABLE IF NOT EXISTS` statements) is not worth
building connection-pooling infrastructure to avoid.

`open_database(..., check_same_thread=False)`: this is *not* only for
the SSE endpoint's long-lived, repeatedly-polled connection (Milestone
15's original reason for adding the flag) -- it turned out every
ordinary route needs it too. FastAPI resolves a sync `yield`
dependency (opening the connection) and runs the sync route handler
(using it) as two *separate* `run_in_threadpool` dispatches; anyio's
thread pool is free to hand each dispatch a different worker thread,
so "open on thread A, use on thread B" is a real, observed failure
mode under genuine concurrent load (`sqlite3.ProgrammingError: SQLite
objects created in a thread can only be used in that same thread`) --
caught during this milestone's own manual browser verification, not
by any single-request-at-a-time test. `check_same_thread=False` is
safe here for the same reason it was already safe for the SSE
connection: access within one request stays strictly sequential (the
dependency fully resolves before the handler runs), never genuinely
concurrent multi-thread access, which is the actual hazard
`check_same_thread` exists to catch.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterator

from fastapi import Request

from argus.api.errors import database_unavailable
from argus.store.database import DatabaseOpenError, SchemaError, open_database
from argus.store.repository import Repository

__all__ = ["get_repository", "get_now"]


def get_repository(request: Request) -> Iterator[Repository]:
    """Opens one connection against `request.app.state.database_path`
    (set once by `create_app`), yields a `Repository` over it, and
    always closes it afterward -- even if the route raises. Used by
    every route, including `argus.api.routes.events`'s SSE endpoint
    (which additionally polls the same connection repeatedly across
    the life of one long-lived request).

    Reuses `open_database` exactly as every ordinary CLI command does,
    so a missing database file is bootstrapped the same established
    way (see that function's own docstring), and a genuinely broken
    database raises the same `DatabaseOpenError`/`SchemaError` the CLI
    already knows how to report -- turned into a clean 503 by
    `database_unavailable` instead of a stack trace.

    Also catches a plain `sqlite3.Error` (e.g. a file that exists but
    isn't a SQLite database at all -- `open_database` itself doesn't
    reach that case until its own `PRAGMA` calls, past its
    `DatabaseOpenError` wrapping) for the same clean-503 treatment: an
    HTTP server handling arbitrary requests against a possibly-external
    file path must never surface a raw driver exception, even for a
    failure mode the CLI's own single-shot-then-exit callers have never
    needed to guard this defensively against.
    """

    try:
        connection = open_database(request.app.state.database_path, check_same_thread=False)
    except (DatabaseOpenError, SchemaError) as exc:
        raise database_unavailable(str(exc)) from exc
    except sqlite3.Error as exc:
        raise database_unavailable(f"could not open database: {exc}") from exc

    try:
        yield Repository(connection)
    finally:
        connection.close()


def get_now() -> datetime:
    """One `now`, read once per request -- the same injected-clock
    discipline `argus.cli.queries` requires of every caller, applied at
    this API's own request boundary instead of the CLI's argv
    boundary."""

    return datetime.now(timezone.utc)
