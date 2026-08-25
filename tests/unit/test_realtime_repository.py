"""Milestone 15 -- `Repository`'s realtime_events methods: monotonic
ids, ascending ordering, replay, retention/pruning, and "reading never
deletes" (multiple SSE consumers must be able to replay the same
history independently).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from argus.store.database import open_database
from argus.store.repository import Repository

UTC = timezone.utc
NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)


def _repo(tmp_path) -> Repository:
    conn = open_database(tmp_path / "a.db")
    return Repository(conn)


class TestEventPersistence:
    def test_ids_are_monotonically_increasing(self, tmp_path):
        repo = _repo(tmp_path)
        ids = [
            repo.insert_realtime_event(
                event_type="collector.tick", occurred_at=NOW, payload_json=json.dumps({"n": i}), created_at=NOW
            )
            for i in range(5)
        ]
        assert ids == sorted(ids)
        assert len(set(ids)) == 5  # all distinct

    def test_round_trips_event_type_and_payload(self, tmp_path):
        repo = _repo(tmp_path)
        repo.insert_realtime_event(
            event_type="incident.opened", occurred_at=NOW,
            payload_json=json.dumps({"incident_id": 14}), created_at=NOW,
        )
        [event] = repo.list_realtime_events_since(after_id=0)
        assert event.event_type == "incident.opened"
        assert json.loads(event.payload_json) == {"incident_id": 14}
        assert event.occurred_at == NOW


class TestEventOrdering:
    def test_listed_ascending_by_id(self, tmp_path):
        repo = _repo(tmp_path)
        for event_type in ("collector.tick", "incident.opened", "incident.resolved"):
            repo.insert_realtime_event(event_type=event_type, occurred_at=NOW, payload_json="{}", created_at=NOW)

        events = repo.list_realtime_events_since(after_id=0)
        assert [e.event_type for e in events] == ["collector.tick", "incident.opened", "incident.resolved"]
        assert [e.id for e in events] == sorted(e.id for e in events)


class TestReplay:
    def test_last_event_id_10_returns_only_11_plus(self, tmp_path):
        repo = _repo(tmp_path)
        ids = [
            repo.insert_realtime_event(event_type="collector.tick", occurred_at=NOW, payload_json="{}", created_at=NOW)
            for _ in range(15)
        ]
        tenth_id = ids[9]  # the 10th inserted row's own id (ids are 1-based sequential in a fresh table, but don't assume literal value 10)

        replayed = repo.list_realtime_events_since(after_id=tenth_id)
        assert [e.id for e in replayed] == ids[10:]
        assert all(e.id > tenth_id for e in replayed)

    def test_after_id_0_returns_everything_retained(self, tmp_path):
        repo = _repo(tmp_path)
        for _ in range(3):
            repo.insert_realtime_event(event_type="collector.tick", occurred_at=NOW, payload_json="{}", created_at=NOW)
        assert len(repo.list_realtime_events_since(after_id=0)) == 3

    def test_limit_bounds_a_single_poll(self, tmp_path):
        repo = _repo(tmp_path)
        for _ in range(10):
            repo.insert_realtime_event(event_type="collector.tick", occurred_at=NOW, payload_json="{}", created_at=NOW)
        assert len(repo.list_realtime_events_since(after_id=0, limit=4)) == 4


class TestMultipleConsumersReplay:
    def test_reading_does_not_delete_events(self, tmp_path):
        repo = _repo(tmp_path)
        repo.insert_realtime_event(event_type="collector.tick", occurred_at=NOW, payload_json="{}", created_at=NOW)

        first_reader = repo.list_realtime_events_since(after_id=0)
        second_reader = repo.list_realtime_events_since(after_id=0)
        assert len(first_reader) == 1
        assert len(second_reader) == 1
        assert first_reader[0].id == second_reader[0].id


class TestRetention:
    def test_prune_keeps_only_the_most_recent_n(self, tmp_path):
        repo = _repo(tmp_path)
        for _ in range(20):
            repo.insert_realtime_event(event_type="collector.tick", occurred_at=NOW, payload_json="{}", created_at=NOW)

        deleted = repo.prune_realtime_events(keep_last=5)
        remaining = repo.list_realtime_events_since(after_id=0)
        assert deleted == 15
        assert len(remaining) == 5
        # the *newest* 5 survive, not an arbitrary 5
        assert min(e.id for e in remaining) > 15  # the first 15 inserted are gone

    def test_prune_is_a_no_op_when_already_under_the_cap(self, tmp_path):
        repo = _repo(tmp_path)
        for _ in range(3):
            repo.insert_realtime_event(event_type="collector.tick", occurred_at=NOW, payload_json="{}", created_at=NOW)

        deleted = repo.prune_realtime_events(keep_last=10_000)
        assert deleted == 0
        assert len(repo.list_realtime_events_since(after_id=0)) == 3

    def test_prune_on_empty_table_is_safe(self, tmp_path):
        repo = _repo(tmp_path)
        assert repo.prune_realtime_events(keep_last=100) == 0


class TestEventIdBounds:
    def test_bounds_on_empty_table(self, tmp_path):
        repo = _repo(tmp_path)
        assert repo.get_realtime_event_id_bounds() == (None, None)

    def test_bounds_reflect_earliest_and_latest_retained(self, tmp_path):
        repo = _repo(tmp_path)
        ids = [
            repo.insert_realtime_event(event_type="collector.tick", occurred_at=NOW, payload_json="{}", created_at=NOW)
            for _ in range(5)
        ]
        assert repo.get_realtime_event_id_bounds() == (ids[0], ids[-1])

    def test_bounds_after_pruning_reflect_the_new_earliest(self, tmp_path):
        repo = _repo(tmp_path)
        ids = [
            repo.insert_realtime_event(event_type="collector.tick", occurred_at=NOW, payload_json="{}", created_at=NOW)
            for _ in range(10)
        ]
        repo.prune_realtime_events(keep_last=3)
        earliest, latest = repo.get_realtime_event_id_bounds()
        assert earliest == ids[-3]
        assert latest == ids[-1]
