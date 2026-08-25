"""Milestone 15 -- realtime events end to end against the real,
disposable `argus-test-stack`: a real Docker container change drives a
real collector tick, which commits real transitions/incidents, which
`argus.realtime.emitter` turns into real `realtime_events` rows, which
`argus.api.routes.events`'s SSE generator can then replay -- exactly
the pipeline the milestone's own "Core Architecture" diagram describes.

The collector side (`CollectorLoop`) and the "SSE consumer" side use
two *separate* SQLite connections to the exact same database file --
deliberately mirroring the real deployment shape (`argus-api` and the
collector process are two separate OS processes sharing one file), not
a single shared connection object. No live HTTP server or browser is
started here: `argus.api.routes.events._event_stream` (the exact
generator the real endpoint returns) is driven directly against the
second connection, which already proves the whole delivery mechanism
without the added complexity/hang-risk of a real network round trip
(see `tests/unit/test_api_events.py` for why that direct-generator
approach was chosen there too).

Only `argus-test-stack`'s own `healthy-api` container is ever
stopped/started -- via `conftest.py`'s `safe_stop`/`safe_start`, which
refuse anything not carrying the test stack's own compose-project
label. No AI call anywhere in this file.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from argus.api.routes.events import _event_stream
from argus.collector.loop import CollectorLoop
from argus.collectors.docker_client import DockerClient
from argus.domain.models import HealthStatus
from argus.store.database import open_database
from argus.store.repository import Repository

from conftest import TEST_PROJECT_NAME, compose_container_id, safe_start, safe_stop, wait_until
from test_chaos_stack import APPLICATION_FAILURE_SIGNATURE, TEST_CONFIG, TEST_RULES, _argus_test_stack_is_healthy

pytestmark = [pytest.mark.integration, pytest.mark.docker]


class _FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


async def _drain(repository: Repository, *, after_id: int, timeout: float = 15.0) -> list[dict]:
    """Polls the SSE generator directly (see this module's own docstring)
    until at least one event newer than `after_id` shows up, then
    returns every event seen as `{"id":..., "event":..., "data": {...}}`
    dicts -- a thin parse of the real SSE wire format, not a
    reimplementation of it."""

    gen = _event_stream(_FakeRequest(), repository, after_id)
    seen: list[dict] = []
    try:
        async with asyncio.timeout(timeout):
            async for chunk in gen:
                text = chunk.decode("utf-8")
                if text.startswith(":"):
                    continue  # heartbeat -- not a real event
                lines = text.strip("\n").split("\n")
                parsed = {"id": None, "event": None, "data": None}
                for line in lines:
                    if line.startswith("id: "):
                        parsed["id"] = int(line.removeprefix("id: "))
                    elif line.startswith("event: "):
                        parsed["event"] = line.removeprefix("event: ")
                    elif line.startswith("data: "):
                        parsed["data"] = json.loads(line.removeprefix("data: "))
                seen.append(parsed)
                if len(seen) >= 1:
                    break
    except TimeoutError:
        # Nothing new arrived within this one poll's budget -- not a
        # failure here, `_drain_until`'s own outer loop decides whether
        # to keep retrying against its own overall deadline.
        pass
    finally:
        await gen.aclose()
    return seen


def _drain_until(repository: Repository, *, after_id: int, event_types: set[str], timeout: float = 20.0) -> list[dict]:
    """Repeatedly drives `_drain` (each call stops at the first real
    event) until every type in `event_types` has been seen at least
    once, or `timeout` elapses. Returns everything collected along the
    way."""

    collected: list[dict] = []
    end = time.monotonic() + timeout
    cursor = after_id
    while time.monotonic() < end:
        remaining = max(0.5, end - time.monotonic())
        batch = asyncio.run(_drain(repository, after_id=cursor, timeout=remaining))
        for event in batch:
            collected.append(event)
            cursor = max(cursor, event["id"])
        if event_types <= {e["event"] for e in collected}:
            break
    return collected


class TestRealtimeEventsFromRealDockerChange:
    def test_stopping_and_restarting_healthy_api_produces_the_expected_events(self, stack, raw_docker, argus_db):
        db_path, collector_conn, collector_repo = argus_db
        client = DockerClient()
        loop = CollectorLoop(client=client, repository=collector_repo, config=TEST_CONFIG, rules=TEST_RULES)

        # A second, independent connection -- the "SSE consumer" side,
        # mirroring a separate argus-api process reading the same file.
        sse_conn = open_database(db_path, check_same_thread=False)
        sse_repo = Repository(sse_conn)

        try:
            baseline = loop.run_once()
            assert baseline.success
            assert collector_repo.get_open_incident(failure_signature=APPLICATION_FAILURE_SIGNATURE) is None
            cursor = sse_repo.get_realtime_event_id_bounds()[1] or 0

            container_id = compose_container_id("healthy-api")
            safe_stop(raw_docker, container_id)
            wait_until(
                lambda: raw_docker.containers.get(container_id).status == "exited",
                timeout=20, interval=1, description="healthy-api reports exited",
            )

            stopped_tick = loop.run_once()
            assert stopped_tick.success
            open_incident = collector_repo.get_open_incident(failure_signature=APPLICATION_FAILURE_SIGNATURE)
            assert open_incident is not None

            events_after_stop = _drain_until(
                sse_repo, after_id=cursor, event_types={"application.status_changed", "incident.opened"},
            )
            event_types_seen = {e["event"] for e in events_after_stop}
            assert "application.status_changed" in event_types_seen
            assert "incident.opened" in event_types_seen

            opened_event = next(e for e in events_after_stop if e["event"] == "incident.opened")
            assert opened_event["data"]["incident_id"] == open_incident.id
            assert opened_event["data"]["application_key"] == TEST_PROJECT_NAME

            status_event = next(e for e in events_after_stop if e["event"] == "application.status_changed")
            assert status_event["data"]["to_status"] in ("UNHEALTHY", "STOPPED")

            cursor = max(e["id"] for e in events_after_stop)

            safe_start(raw_docker, container_id)
            wait_until(
                lambda: _argus_test_stack_is_healthy(client),
                timeout=30, interval=2, description="argus-test-stack fully healthy again",
            )

            recovered_tick = loop.run_once()
            assert recovered_tick.success
            assert collector_repo.get_open_incident(failure_signature=APPLICATION_FAILURE_SIGNATURE) is None

            events_after_recovery = _drain_until(
                sse_repo, after_id=cursor, event_types={"application.status_changed", "incident.resolved"},
            )
            recovery_types_seen = {e["event"] for e in events_after_recovery}
            assert "application.status_changed" in recovery_types_seen
            assert "incident.resolved" in recovery_types_seen

            resolved_event = next(e for e in events_after_recovery if e["event"] == "incident.resolved")
            assert resolved_event["data"]["incident_id"] == open_incident.id

            recovery_status_event = next(e for e in events_after_recovery if e["event"] == "application.status_changed")
            assert recovery_status_event["data"]["to_status"] == "HEALTHY"

            # No payload anywhere in this whole scenario ever carries a raw
            # log sample, a secret, or a Docker label -- only ids/keys/
            # statuses (see argus.realtime.emitter's own sanitization).
            for event in events_after_stop + events_after_recovery:
                serialized = json.dumps(event["data"])
                for forbidden in ("password", "SECRET", "/var/run/docker.sock", "Env"):
                    assert forbidden not in serialized
        finally:
            sse_conn.close()
            wait_until(
                lambda: _argus_test_stack_is_healthy(DockerClient()),
                timeout=30, interval=2, description="argus-test-stack healthy before next test",
                on_timeout=lambda: "leaving healthy-api in whatever state it is in -- see the failure above",
            )
