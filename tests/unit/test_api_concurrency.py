"""Regression test for a real bug found during Milestone 15's own
manual browser verification (not caught by any single-request-at-a-time
test before it, and -- see below -- not reliably reproducible through
`fastapi.testclient.TestClient` either): FastAPI resolves a sync
`yield` dependency (opening the SQLite connection) and runs the sync
route handler (using it) as two *separate* `run_in_threadpool`
dispatches. Under genuine concurrent load against a real running
server, anyio's thread pool can hand those two dispatches different
worker threads, and a `sqlite3.Connection` opened with the default
`check_same_thread=True` then raises `sqlite3.ProgrammingError: SQLite
objects created in a thread can only be used in that same thread` --
surfaced to the client as a raw 500.

Fixed in `argus.api.dependencies.get_repository` via
`open_database(..., check_same_thread=False)` (see that function's own
docstring for the full explanation).

`TestClient` funnels every request through one blocking portal into a
single event loop, which was observed to reuse the same worker thread
across `run_in_threadpool` dispatches regardless of how many separate
Python threads submit requests to it -- so a `ThreadPoolExecutor`-based
test against `TestClient` does *not* reliably reproduce this bug (it
was verified to pass even with the old, broken `check_same_thread=True`
code during this fix's own development). Because of that, this test
instead reproduces the exact underlying condition deterministically,
at the mechanism level `open_database` itself provides -- open the
connection on one real OS thread, use it from a genuinely different
one, exactly as FastAPI's two separate threadpool dispatches do --
rather than depending on a specific ASGI test harness's own thread-
reuse behavior to happen to trigger it.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from argus.store.database import open_database


def test_check_same_thread_false_allows_use_from_a_different_thread_than_it_was_opened_on(tmp_path):
    """The exact scenario `argus.api.dependencies.get_repository` relies
    on: `open_database(..., check_same_thread=False)` is called on one
    thread, and the returned connection is queried from a different
    one (this test's own main thread) -- must not raise."""

    db_path = tmp_path / "a.db"

    with ThreadPoolExecutor(max_workers=1) as opener_pool:
        connection = opener_pool.submit(open_database, db_path, check_same_thread=False).result()

    connection.execute("SELECT 1").fetchone()  # must not raise sqlite3.ProgrammingError
    connection.close()


def test_check_same_thread_true_the_old_default_actually_does_raise_cross_thread(tmp_path):
    """Sanity check that this test file is actually exercising the real
    hazard, not a no-op: the *default* (`check_same_thread=True`, what
    `get_repository` used before this fix) must still fail the same
    cross-thread access -- proving the fix's `check_same_thread=False`
    is what's actually preventing the failure above, not some
    unrelated factor."""

    db_path = tmp_path / "a.db"

    with ThreadPoolExecutor(max_workers=1) as opener_pool:
        connection = opener_pool.submit(open_database, db_path).result()  # default: check_same_thread=True

    with pytest.raises(sqlite3.ProgrammingError, match="SQLite objects created in a thread"):
        connection.execute("SELECT 1").fetchone()
