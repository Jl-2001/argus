"""GET /api/v1/events -- Server-Sent Events, the one live server-to-browser
channel in Argus (see the milestone's own "Why SSE" section: there is
no browser-to-server real-time control need, so SSE, not WebSockets).

This route is read-only like every other one in this API: it never
writes to `realtime_events` (only `argus.realtime.emitter` does, from
the collector/explanation-generation side), never mutates anything, and
never leaks anything beyond what `argus.realtime.emitter` already
sanitized into each row's `payload_json` -- this module just polls
`realtime_events` on a short interval and formats each row as one SSE
message. Uses the same `get_repository` dependency every other route
does; see `argus.store.database.open_database`'s own docstring for why
its connection is opened with `check_same_thread=False`.

SSE events are NOT authoritative state -- see the milestone's own "Core
Architecture" section. A message here only ever means "something
changed"; the frontend is expected to invalidate/refetch the relevant
GET endpoint for the actual, current truth. Nothing streamed here is
ever a substitute for calling `GET /api/v1/applications/{key}` etc.
"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from argus.api.dependencies import get_repository
from argus.realtime.events import HEARTBEAT_INTERVAL_SECONDS, POLL_INTERVAL_SECONDS, SCHEMA_VERSION
from argus.store.repository import RealtimeEventRecord, Repository

router = APIRouter()

_POLL_LIMIT = 200  # events per DB poll -- bounded, never "however many exist"


def _format_event(record: RealtimeEventRecord) -> bytes:
    """One `realtime_events` row as one SSE message. `record.payload_json`
    is already-serialized, already-sanitized JSON text (see
    `argus.realtime.emitter`) -- written straight into the `data:` line,
    never re-parsed/re-serialized here."""

    lines = [f"id: {record.id}", f"event: {record.event_type}", f"data: {record.payload_json}", "", ""]
    return "\n".join(lines).encode("utf-8")


def _format_control_event(event: str, data: dict) -> bytes:
    lines = [f"event: {event}", f"data: {json.dumps(data, sort_keys=True)}", "", ""]
    return "\n".join(lines).encode("utf-8")


def _format_heartbeat() -> bytes:
    # A comment line (starts with ':'), per the SSE spec -- transport
    # level only, never a realtime_events row, never surfaced to
    # EventSource's own `onmessage`/named-event listeners.
    return b": heartbeat\n\n"


async def _resolve_start_cursor(repository: Repository, *, last_event_id: "int | None") -> tuple[int, bool]:
    """Returns `(cursor, history_unavailable)`. `cursor` is the id to
    start streaming *after*. A fresh connect (no `Last-Event-ID`) starts
    from whatever is currently latest -- new events only, no backlog
    (see this module's own docstring: SSE is not a state snapshot, so a
    fresh page load has no reason to replay history it doesn't need). A
    genuine reconnect (`Last-Event-ID` present) replays from there,
    unless retention has already dropped that id -- then `cursor` is the
    current latest and `history_unavailable=True`, signaling the caller
    to emit `stream.reset` before continuing.
    """

    earliest_id, latest_id = await run_in_threadpool(repository.get_realtime_event_id_bounds)
    latest_id = latest_id or 0

    if last_event_id is None:
        return latest_id, False

    if earliest_id is not None and last_event_id < earliest_id - 1:
        return latest_id, True

    return last_event_id, False


async def _event_stream(request: Request, repository: Repository, last_event_id: "int | None"):
    cursor, history_unavailable = await _resolve_start_cursor(repository, last_event_id=last_event_id)
    if history_unavailable:
        yield _format_control_event(
            "stream.reset", {"schema_version": SCHEMA_VERSION, "reason": "history_unavailable"}
        )

    last_heartbeat = time.monotonic()
    while True:
        if await request.is_disconnected():
            break

        rows = await run_in_threadpool(repository.list_realtime_events_since, after_id=cursor, limit=_POLL_LIMIT)
        if rows:
            for row in rows:
                yield _format_event(row)
                cursor = row.id
            continue  # more may already be waiting -- keep draining before sleeping

        now = time.monotonic()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
            yield _format_heartbeat()
            last_heartbeat = now

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def _parse_last_event_id(raw: "str | None") -> "int | None":
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None  # malformed header -- treated as "no Last-Event-ID", never a 400 for a best-effort reconnect hint


@router.get(
    "/events",
    summary="Live server-to-browser event stream (Server-Sent Events)",
    description=(
        "text/event-stream. Each message is `id: <sequence>\\nevent: <type>\\ndata: <json>\\n\\n`. "
        "Event types: collector.tick, application.status_changed, service.status_changed, "
        "container.status_changed, incident.opened, incident.updated, incident.resolved, "
        "evidence.updated, evidence.health_changed, explanation.available. A `: heartbeat` comment "
        "is sent on an otherwise-idle connection every ~15s. Reconnecting with a `Last-Event-ID` "
        "header replays events after that id; if retention has dropped that far back, a "
        "`stream.reset` control event is sent first and the client should invalidate/refetch "
        "everything. Events are never authoritative state -- always refetch the corresponding "
        "GET endpoint."
    ),
)
async def stream_events(
    request: Request, repository: Repository = Depends(get_repository)
) -> StreamingResponse:
    last_event_id = _parse_last_event_id(request.headers.get("last-event-id"))
    return StreamingResponse(
        _event_stream(request, repository, last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable reverse-proxy buffering (nginx etc.), if one ever sits in front
        },
    )
