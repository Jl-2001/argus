"""Milestone 15 -- GET /api/v1/events: SSE formatting, replay,
retention-gap `stream.reset`, heartbeats, and client-disconnect
cleanup.

Every async piece here uses a plain `asyncio.run(...)` inside an
ordinary sync test function -- no new pytest-asyncio/anyio-plugin
dependency needed for a handful of focused async tests. Streaming
response bodies are tested by driving `_event_stream` (the actual
async generator the route returns) directly rather than through a real
HTTP client -- faster, and avoids any transport-layer ambiguity about
when a streamed response is considered "done" for a test.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from argus.api.routes import events as events_module
from argus.store.database import open_database
from argus.store.repository import Repository

UTC = timezone.utc
NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)


def _repo(tmp_path) -> Repository:
    conn = open_database(tmp_path / "a.db", check_same_thread=False)
    return Repository(conn)


def _insert(repo: Repository, event_type: str, payload: dict) -> int:
    return repo.insert_realtime_event(
        event_type=event_type, occurred_at=NOW, payload_json=json.dumps(payload), created_at=NOW
    )


class _FakeRequest:
    def __init__(self, disconnected: bool = False):
        self._disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self._disconnected


async def _collect(agen, n: int, timeout: float = 3.0) -> list[bytes]:
    chunks: list[bytes] = []
    for _ in range(n):
        chunks.append(await asyncio.wait_for(agen.__anext__(), timeout=timeout))
    return chunks


class TestSSEFormat:
    def test_id_event_data_lines_and_blank_line_terminator(self, tmp_path):
        repo = _repo(tmp_path)
        _insert(repo, "incident.opened", {"schema_version": 1, "incident_id": 14})
        [record] = repo.list_realtime_events_since(after_id=0)

        chunk = events_module._format_event(record).decode("utf-8")
        lines = chunk.split("\n")
        assert lines[0] == f"id: {record.id}"
        assert lines[1] == "event: incident.opened"
        assert json.loads(lines[2].removeprefix("data: ")) == {"schema_version": 1, "incident_id": 14}
        assert chunk.endswith("\n\n")  # the blank-line message terminator

    def test_control_event_format(self):
        chunk = events_module._format_control_event("stream.reset", {"reason": "history_unavailable"}).decode()
        assert "event: stream.reset" in chunk
        assert '"reason": "history_unavailable"' in chunk
        assert not chunk.startswith("id:")
        assert chunk.endswith("\n\n")

    def test_heartbeat_is_a_comment_line(self):
        chunk = events_module._format_heartbeat()
        assert chunk.startswith(b":")
        assert chunk.endswith(b"\n\n")


class TestLastEventIdParsing:
    def test_valid_integer(self):
        assert events_module._parse_last_event_id("42") == 42

    def test_missing_header(self):
        assert events_module._parse_last_event_id(None) is None

    def test_malformed_value_treated_as_absent(self):
        assert events_module._parse_last_event_id("not-a-number") is None


class TestReplay:
    def test_fresh_connect_starts_at_latest_no_backlog(self, tmp_path):
        repo = _repo(tmp_path)
        _insert(repo, "collector.tick", {})
        _insert(repo, "collector.tick", {})

        async def body():
            cursor, reset = await events_module._resolve_start_cursor(repo, last_event_id=None)
            return cursor, reset

        cursor, reset = asyncio.run(body())
        assert reset is False
        assert cursor == repo.get_realtime_event_id_bounds()[1]  # latest id -- nothing to replay

    def test_reconnect_with_last_event_id_10_replays_from_11(self, tmp_path):
        repo = _repo(tmp_path)
        ids = [_insert(repo, "collector.tick", {"n": i}) for i in range(15)]
        tenth = ids[9]

        async def body():
            return await events_module._event_stream(_FakeRequest(), repo, tenth).__anext__()

        chunk = asyncio.run(body())
        # first replayed event must be the one right after id=tenth
        first_line = chunk.decode().split("\n")[0]
        assert first_line == f"id: {ids[10]}"

    def test_full_replay_sequence_is_in_order(self, tmp_path):
        repo = _repo(tmp_path)
        ids = [_insert(repo, "collector.tick", {"n": i}) for i in range(5)]

        async def body():
            gen = events_module._event_stream(_FakeRequest(), repo, 0)
            chunks = await _collect(gen, 5)
            await gen.aclose()
            return chunks

        chunks = asyncio.run(body())
        seen_ids = [int(c.decode().split("\n")[0].removeprefix("id: ")) for c in chunks]
        assert seen_ids == ids


class TestRetentionResetOnGap:
    def test_last_event_id_older_than_retention_triggers_reset(self, tmp_path):
        repo = _repo(tmp_path)
        for _ in range(20):
            _insert(repo, "collector.tick", {})
        repo.prune_realtime_events(keep_last=5)
        earliest, latest = repo.get_realtime_event_id_bounds()

        async def body():
            return await events_module._resolve_start_cursor(repo, last_event_id=1)  # long gone

        cursor, reset = asyncio.run(body())
        assert reset is True
        assert cursor == latest

    def test_stream_emits_reset_control_event_first_on_a_gap(self, tmp_path):
        repo = _repo(tmp_path)
        for _ in range(20):
            _insert(repo, "collector.tick", {})
        repo.prune_realtime_events(keep_last=5)

        async def body():
            gen = events_module._event_stream(_FakeRequest(), repo, 1)
            chunk = await asyncio.wait_for(gen.__anext__(), timeout=3)
            await gen.aclose()
            return chunk

        chunk = asyncio.run(body()).decode()
        assert "event: stream.reset" in chunk
        assert "history_unavailable" in chunk

    def test_no_gap_when_last_event_id_is_within_retained_range(self, tmp_path):
        repo = _repo(tmp_path)
        ids = [_insert(repo, "collector.tick", {}) for _ in range(10)]
        repo.prune_realtime_events(keep_last=8)

        async def body():
            return await events_module._resolve_start_cursor(repo, last_event_id=ids[-8])

        cursor, reset = asyncio.run(body())
        assert reset is False


class TestHeartbeat:
    def test_heartbeat_sent_on_idle_connection_without_a_db_row(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        # Speed the test up -- no events, and a tiny heartbeat interval.
        monkeypatch.setattr(events_module, "HEARTBEAT_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(events_module, "POLL_INTERVAL_SECONDS", 0.01)

        async def body():
            gen = events_module._event_stream(_FakeRequest(), repo, 0)
            chunk = await asyncio.wait_for(gen.__anext__(), timeout=3)
            await gen.aclose()
            return chunk

        chunk = asyncio.run(body())
        assert chunk == b": heartbeat\n\n"
        # heartbeats are transport-level only -- never a realtime_events row
        assert repo.get_realtime_event_id_bounds() == (None, None)


class TestSanitizationEndToEnd:
    """`test_realtime_emitter.py::TestSanitization` already proves no
    emitter payload can contain forbidden content; this proves the same
    thing one layer further out -- through the actual formatted SSE
    bytes `GET /api/v1/events` would send a browser."""

    _FORBIDDEN_SNIPPETS = ("DATABASE_URL", "sk-ant-", "AIza", "/var/run/docker.sock", "SYSTEM PROMPT")

    def test_formatted_sse_output_never_contains_forbidden_content(self, tmp_path):
        from argus.domain.models import HealthStatus
        from argus.incidents.engine import IncidentOpened, IncidentProcessingResult, TransitionOccurred
        from argus.realtime import emitter

        repo = _repo(tmp_path)
        emitter.emit_collector_tick(repo, success=True, tick_at=NOW, applications=1, observations=1, now=NOW)
        emitter.emit_incident_processing_events(
            repo, now=NOW,
            result=IncidentProcessingResult(
                transitions_created=1, incidents_opened=1, incidents_updated=0, incidents_resolved=0,
                transitions=(TransitionOccurred(scope="application", scope_id=1, application_key="cnstrct", from_status=HealthStatus.HEALTHY, to_status=HealthStatus.UNHEALTHY, transition_id=1, occurred_at=NOW),),
                opened_incidents=(IncidentOpened(incident_id=1, application_key="cnstrct", opening_status=HealthStatus.UNHEALTHY),),
            ),
        )
        emitter.emit_explanation_available(repo, incident_id=1, provider="anthropic", model="claude-sonnet-5", bundle_fingerprint="fp", now=NOW)

        async def body():
            gen = events_module._event_stream(_FakeRequest(), repo, 0)
            chunks = await _collect(gen, 3)
            await gen.aclose()
            return chunks

        for chunk in asyncio.run(body()):
            text = chunk.decode("utf-8")
            for snippet in self._FORBIDDEN_SNIPPETS:
                assert snippet not in text


class TestClientDisconnect:
    def test_stream_stops_cleanly_when_already_disconnected(self, tmp_path):
        repo = _repo(tmp_path)

        async def body():
            gen = events_module._event_stream(_FakeRequest(disconnected=True), repo, 0)
            got_anything = False
            async for _chunk in gen:
                got_anything = True
                break
            return got_anything

        # The generator must terminate (StopAsyncIteration) rather than
        # hang or yield anything, once the request is already disconnected.
        got_anything = asyncio.run(asyncio.wait_for(body(), timeout=3))
        assert got_anything is False
