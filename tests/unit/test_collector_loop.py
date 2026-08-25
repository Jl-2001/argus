"""Tests for argus.collector.loop (and the schema v1 -> v2 migration it
motivated). run_once() is the main target; run_forever() is tested only
through injected clock/sleep and small tick budgets -- nothing here
waits on real time or touches a real Docker daemon.
"""

from __future__ import annotations

import ast
import inspect
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import docker.errors
import pytest

from argus.collector import loop as loop_module
from argus.collector.loop import (
    CollectorConfig,
    CollectorLoop,
    MissingEvaluationError,
    TickResult,
    compute_backoff,
)
from argus.collectors.docker_client import ContainerAttrs, DockerClient, DockerUnavailableError
from argus.collectors.docker_collector import discover
from argus.domain.models import HealthStatus
from argus.evidence.collector import EvidenceCollectionLimits
from argus.store.database import PersistenceError, SchemaError, open_database
from argus.store.repository import Repository, resolve_observation_health

UTC = timezone.utc
TICK_AT = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "docker_responses"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


# --------------------------------------------------------------------------
# Fake Docker SDK plumbing (self-contained, mirroring test_discovery.py's
# pattern rather than importing its private helpers)
# --------------------------------------------------------------------------


class _FakeContainer:
    def __init__(self, id: str, attrs: dict):
        self.id = id
        self.attrs = attrs


class _FakeContainersAPI:
    def __init__(self, by_id: dict[str, dict], list_error: BaseException | None = None):
        self._by_id = by_id
        self._list_error = list_error

    def list(self, all=False):
        if self._list_error is not None:
            raise self._list_error
        return [_FakeContainer(cid, {}) for cid in self._by_id]

    def get(self, container_id):
        return _FakeContainer(container_id, self._by_id[container_id])


class _FakeSDKClient:
    def __init__(self, by_id: dict[str, dict], list_error: BaseException | None = None):
        self.containers = _FakeContainersAPI(by_id, list_error=list_error)


def make_fixture_client(fixture_names: list[str]) -> DockerClient:
    by_id = {}
    for name in fixture_names:
        attrs = load_fixture(name)
        by_id[attrs["Id"]] = attrs
    return DockerClient(client=_FakeSDKClient(by_id))


def make_unavailable_client() -> DockerClient:
    return DockerClient(client=_FakeSDKClient({}, list_error=docker.errors.DockerException("daemon down")))


def open_repo(tmp_path, name: str = "a.db") -> tuple[sqlite3.Connection, Repository]:
    conn = open_database(tmp_path / name)
    return conn, Repository(conn)


class _ScriptedDockerClient:
    """A DockerClient-like test double whose list_containers() follows a
    scripted sequence of outcomes -- 'fail' or a list of fixture names --
    one outcome consumed per discover() call. Used only to test the loop's
    scheduling/backoff behavior across several ticks; discovery parsing
    correctness itself is already covered in test_discovery.py."""

    def __init__(self, outcomes: list):
        self._outcomes = outcomes
        self._call_count = 0
        self._current: list[str] | None = None

    def list_containers(self, all=True):
        outcome = self._outcomes[self._call_count]
        self._call_count += 1
        self._current = outcome
        if outcome == "fail":
            raise DockerUnavailableError("scripted failure")
        return [load_fixture(name)["Id"] for name in outcome]

    def inspect_container(self, container_id):
        for name in self._current or ():
            attrs = load_fixture(name)
            if attrs["Id"] == container_id:
                return ContainerAttrs(id=container_id, attrs=attrs)
        raise AssertionError(f"unexpected inspect_container call for {container_id!r}")


def make_advancing_clock(start: datetime, step_seconds: float = 15.0):
    """A clock that moves forward by `step_seconds` on every call -- real
    ticks each have a genuinely later observed_at than the last, avoiding a
    same-timestamp (container, observed_at) collision across ticks that a
    single fixed clock would cause once observations are actually persisted."""

    state = {"t": start}

    def clock() -> datetime:
        current = state["t"]
        state["t"] = current + timedelta(seconds=step_seconds)
        return current

    return clock


class _RecordingSleep:
    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


# --------------------------------------------------------------------------
# Schema migration (v1 -> v2, introduced by this milestone)
# --------------------------------------------------------------------------


class TestSchemaMigrationV1ToV2:
    def test_fresh_database_lands_on_v2_with_collector_state(self, tmp_path):
        # SCHEMA_VERSION moved to 3 in Milestone 6 (health_transitions/
        # incidents); a fresh database still gets collector_state along
        # the way, which is the thing this test actually verifies.
        conn = open_database(tmp_path / "a.db")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8  # SCHEMA_VERSION moved to 8 in Milestone 16 (multi-host agents)
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "collector_state" in tables
        conn.close()

    def test_existing_v1_database_migrates_without_losing_data(self, tmp_path):
        db_path = tmp_path / "a.db"

        # A genuine pre-Milestone-5 database: only the v1 tables, no
        # collector_state, user_version = 1.
        v1_conn = sqlite3.connect(str(db_path))
        v1_conn.executescript(
            """
            CREATE TABLE applications (
                id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                is_standalone INTEGER NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
            );
            """
        )
        v1_conn.execute(
            "INSERT INTO applications (key, name, is_standalone, first_seen_at, last_seen_at) "
            "VALUES ('cnstrct','CNSTRCT',0,?,?)",
            (TICK_AT.isoformat(), TICK_AT.isoformat()),
        )
        v1_conn.execute("PRAGMA user_version = 1")
        v1_conn.commit()
        v1_conn.close()

        conn = open_database(db_path)
        # walks v1 -> v2 -> v3 -> v4 in one open_database() call
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8  # SCHEMA_VERSION moved to 8 in Milestone 16 (multi-host agents)
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "collector_state" in tables

        row = conn.execute("SELECT * FROM applications WHERE key = 'cnstrct'").fetchone()
        assert row["name"] == "CNSTRCT"
        assert row["first_seen_at"] == TICK_AT.isoformat()
        conn.close()

    def test_existing_current_version_database_opens_safely(self, tmp_path):
        # A fresh open_database() now lands directly on v4 (Milestone 10),
        # so this exercises "reopening an already-current-version database
        # doesn't lose data" rather than a literal v2 database specifically
        # -- the genuine v1 -> v4 migration path is covered by the test
        # above, v2 -> v3 specifically is covered in
        # test_incident_engine.py, and v3 -> v4 specifically is covered in
        # test_repository.py's TestSchemaMigrationV3ToV4.
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        Repository(conn).upsert_application(
            key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=TICK_AT
        )
        conn.close()

        conn2 = open_database(db_path)
        assert conn2.execute("PRAGMA user_version").fetchone()[0] == 8  # SCHEMA_VERSION moved to 8 in Milestone 16 (multi-host agents)
        assert Repository(conn2).get_application("cnstrct") is not None
        conn2.close()

    def test_unsupported_future_version_raises_and_does_not_downgrade(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        conn.execute("PRAGMA user_version = 99")
        conn.close()

        with pytest.raises(SchemaError):
            open_database(db_path)


# --------------------------------------------------------------------------
# Collector heartbeat (repository-level)
# --------------------------------------------------------------------------


class TestCollectorStateRepository:
    def test_fresh_state_is_all_empty(self, tmp_path):
        _, repo = open_repo(tmp_path)
        state = repo.get_collector_state()
        assert state.last_tick_at is None
        assert state.last_success_at is None
        assert state.consecutive_failures == 0
        assert state.last_error is None

    def test_record_tick_started_updates_last_tick_at_only(self, tmp_path):
        _, repo = open_repo(tmp_path)
        repo.record_tick_started(at=TICK_AT)
        state = repo.get_collector_state()
        assert state.last_tick_at == TICK_AT
        assert state.last_success_at is None
        assert state.consecutive_failures == 0

    def test_record_tick_success_sets_success_and_resets_failures(self, tmp_path):
        _, repo = open_repo(tmp_path)
        repo.record_tick_started(at=TICK_AT)
        repo.record_tick_failure(error="boom")
        repo.record_tick_started(at=TICK_AT)
        repo.record_tick_success(at=TICK_AT)

        state = repo.get_collector_state()
        assert state.last_success_at == TICK_AT
        assert state.consecutive_failures == 0
        assert state.last_error is None

    def test_record_tick_failure_increments_and_sets_error(self, tmp_path):
        _, repo = open_repo(tmp_path)
        repo.record_tick_started(at=TICK_AT)
        count = repo.record_tick_failure(error="RuntimeError: boom")
        assert count == 1
        state = repo.get_collector_state()
        assert state.consecutive_failures == 1
        assert state.last_error == "RuntimeError: boom"
        assert state.last_success_at is None  # never advanced by a failure

    def test_repeated_failures_keep_incrementing(self, tmp_path):
        _, repo = open_repo(tmp_path)
        for _ in range(3):
            repo.record_tick_failure(error="still down")
        assert repo.get_collector_state().consecutive_failures == 3

    def test_success_after_failures_clears_error_and_resets_count(self, tmp_path):
        _, repo = open_repo(tmp_path)
        repo.record_tick_failure(error="down")
        repo.record_tick_failure(error="still down")
        repo.record_tick_success(at=TICK_AT)
        state = repo.get_collector_state()
        assert state.consecutive_failures == 0
        assert state.last_error is None
        assert state.last_success_at == TICK_AT


# --------------------------------------------------------------------------
# compute_backoff
# --------------------------------------------------------------------------


class TestComputeBackoff:
    @pytest.mark.parametrize(
        "failures, expected",
        [(1, 15.0), (2, 30.0), (3, 60.0), (4, 60.0), (5, 60.0)],
    )
    def test_exact_sequence(self, failures, expected):
        assert compute_backoff(failures) == expected

    def test_zero_failures_is_zero(self):
        assert compute_backoff(0) == 0.0

    def test_custom_config(self):
        config = CollectorConfig(poll_interval=15, backoff_initial=5, backoff_max=20)
        assert compute_backoff(1, config) == 5.0
        assert compute_backoff(2, config) == 10.0
        assert compute_backoff(3, config) == 20.0
        assert compute_backoff(4, config) == 20.0

    def test_config_rejects_nonpositive_poll_interval(self):
        with pytest.raises(ValueError):
            CollectorConfig(poll_interval=0)

    def test_config_rejects_backoff_max_below_initial(self):
        with pytest.raises(ValueError):
            CollectorConfig(backoff_initial=30, backoff_max=15)


# --------------------------------------------------------------------------
# run_once
# --------------------------------------------------------------------------


class TestRunOnce:
    def test_successful_tick_persists_and_records_success(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        client = make_fixture_client(["compose_healthy_api", "compose_postgres_nohealthcheck"])
        collector = CollectorLoop(client=client, repository=repo, clock=lambda: TICK_AT)

        result = collector.run_once()

        assert result.success is True
        assert result.applications == 1
        assert result.observations == 2
        assert result.error is None

        app = repo.get_application("cnstrct")
        assert app is not None
        state = repo.get_collector_state()
        assert state.last_success_at == TICK_AT
        assert state.consecutive_failures == 0
        conn.close()

    def test_empty_daemon_is_a_successful_tick(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        client = make_fixture_client([])
        collector = CollectorLoop(client=client, repository=repo, clock=lambda: TICK_AT)

        result = collector.run_once()

        assert result == TickResult(
            success=True, applications=0, services=0, containers=0, observations=0, skipped=0, error=None
        )
        state = repo.get_collector_state()
        assert state.last_success_at == TICK_AT
        assert state.consecutive_failures == 0
        conn.close()

    def test_docker_unavailable_fails_tick_without_advancing_success(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        client = make_unavailable_client()
        collector = CollectorLoop(client=client, repository=repo, clock=lambda: TICK_AT)

        result = collector.run_once()

        assert result.success is False
        assert result.observations == 0
        assert "DockerUnavailableError" in result.error

        state = repo.get_collector_state()
        assert state.last_tick_at == TICK_AT  # still updated even on failure
        assert state.last_success_at is None
        assert state.consecutive_failures == 1
        conn.close()

    def test_persistence_failure_does_not_record_success(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        client = make_fixture_client(["compose_healthy_api"])

        # Pre-seed the exact same snapshot directly, so the loop's own
        # attempt to persist an identical (container, observed_at) collides.
        seed_result = discover(client, observed_at=TICK_AT)
        seed_observations = [
            resolve_observation_health(
                o, status=seed_result.evaluations[o.container_ref.container_id].status,
                detail=seed_result.evaluations[o.container_ref.container_id].detail,
            )
            for o in seed_result.observations
        ]
        repo.persist_discovery(applications=seed_result.applications, observations=seed_observations)

        collector = CollectorLoop(client=client, repository=repo, clock=lambda: TICK_AT)
        result = collector.run_once()

        assert result.success is False
        assert "Duplicate" in result.error or "duplicate" in result.error

        state = repo.get_collector_state()
        assert state.last_success_at is None  # the loop's own tick never succeeded
        assert state.consecutive_failures == 1
        conn.close()

    def test_partial_discovery_with_skipped_container_still_succeeds(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        client = make_fixture_client(
            ["compose_healthy_api", "musipal_api_healthy", "malformed_unknown_state"]
        )
        collector = CollectorLoop(client=client, repository=repo, clock=lambda: TICK_AT)

        result = collector.run_once()

        assert result.success is True
        assert result.applications == 2
        assert result.skipped == 1
        state = repo.get_collector_state()
        assert state.consecutive_failures == 0
        assert state.last_success_at == TICK_AT
        conn.close()

    def test_missing_evaluation_mapping_fails_tick_and_persists_nothing(self, tmp_path, monkeypatch):
        conn, repo = open_repo(tmp_path)
        client = make_fixture_client(["compose_healthy_api"])

        real_result = discover(client, observed_at=TICK_AT)
        container_id = real_result.observations[0].container_ref.container_id

        # Simulate a DiscoveryResult that is internally inconsistent: an
        # observation exists but its evaluation was dropped.
        broken_result = SimpleNamespace(
            applications=real_result.applications,
            observations=real_result.observations,
            evaluations={},
            skipped=real_result.skipped,
        )
        monkeypatch.setattr(loop_module, "discover", lambda *a, **k: broken_result)

        collector = CollectorLoop(client=client, repository=repo, clock=lambda: TICK_AT)
        result = collector.run_once()

        assert result.success is False
        assert "no HealthEvaluation" in result.error
        assert repo.get_container_by_docker_id(container_id) is None  # nothing persisted
        assert repo.get_collector_state().last_success_at is None
        conn.close()

    def test_missing_evaluation_error_is_the_documented_type(self):
        assert issubclass(MissingEvaluationError, RuntimeError)


# --------------------------------------------------------------------------
# Milestone 15 -- realtime event emission from a real tick, and the one
# guarantee that matters most: a broken realtime_events write must never
# roll back (or even affect) core monitoring state.
# --------------------------------------------------------------------------


class TestRealtimeEventEmission:
    def test_successful_tick_emits_a_collector_tick_event(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        client = make_fixture_client(["compose_healthy_api"])
        collector = CollectorLoop(client=client, repository=repo, clock=lambda: TICK_AT)

        result = collector.run_once()
        assert result.success is True

        events = repo.list_realtime_events_since(after_id=0)
        tick_events = [e for e in events if e.event_type == "collector.tick"]
        assert len(tick_events) == 1
        payload = json.loads(tick_events[0].payload_json)
        assert payload["success"] is True
        conn.close()

    def test_a_new_application_emits_an_application_status_changed_event(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        client = make_fixture_client(["compose_healthy_api"])
        collector = CollectorLoop(client=client, repository=repo, clock=lambda: TICK_AT)

        collector.run_once()

        events = repo.list_realtime_events_since(after_id=0)
        app_events = [e for e in events if e.event_type == "application.status_changed"]
        assert len(app_events) == 1
        payload = json.loads(app_events[0].payload_json)
        assert payload["application_key"] == "cnstrct"
        assert payload["to_status"] == "HEALTHY"
        conn.close()

    def test_realtime_event_write_failure_never_rolls_back_core_tick_state(self, tmp_path, monkeypatch):
        """The whole point of argus.realtime.emitter's own failure
        isolation, proven here from the collector loop's own point of
        view: even if *every* realtime_events write raises, the tick
        must still succeed, observations/transitions/incidents must
        still persist, and collector_state must still advance -- exactly
        as if the realtime layer didn't exist at all.
        """

        conn, repo = open_repo(tmp_path)
        client = make_fixture_client(["compose_healthy_api"])

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated realtime_events write failure (e.g. disk full)")

        monkeypatch.setattr(Repository, "insert_realtime_event", _boom)

        collector = CollectorLoop(client=client, repository=repo, clock=lambda: TICK_AT)
        result = collector.run_once()

        assert result.success is True
        assert result.applications == 1
        assert result.error is None

        app = repo.get_application("cnstrct")
        assert app is not None
        state = repo.get_collector_state()
        assert state.last_success_at == TICK_AT
        assert state.consecutive_failures == 0

        # And -- since every insert attempt raised -- genuinely no
        # realtime_events rows exist; the failure was fully isolated,
        # not silently half-applied.
        assert repo.list_realtime_events_since(after_id=0) == ()
        conn.close()


# --------------------------------------------------------------------------
# Host wiring (Milestone 16)
# --------------------------------------------------------------------------


class TestHostWiring:
    def test_local_applications_are_persisted_under_the_local_host(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        client = make_fixture_client(["compose_healthy_api"])
        collector = CollectorLoop(client=client, repository=repo, clock=lambda: TICK_AT)

        collector.run_once()

        app = repo.get_application("cnstrct")
        local_host = repo.get_host_by_key("local")
        assert app.host_id == local_host.id
        conn.close()

    def test_application_key_is_unscoped_for_the_local_host(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        client = make_fixture_client(["compose_healthy_api"])
        collector = CollectorLoop(client=client, repository=repo, clock=lambda: TICK_AT)

        collector.run_once()

        # Byte-for-byte the same key a pre-Milestone-16 single-host
        # installation always used -- no "local:" prefix anywhere.
        assert repo.get_application("cnstrct") is not None
        conn.close()

    def test_custom_host_display_name_is_applied(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        client = make_fixture_client(["compose_healthy_api"])
        collector = CollectorLoop(
            client=client, repository=repo, clock=lambda: TICK_AT, host_display_name="MacBook"
        )

        collector.run_once()

        local_host = repo.get_host_by_key("local")
        assert local_host.display_name == "MacBook"
        conn.close()

    def test_local_host_heartbeat_advances_on_every_tick(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        client = make_fixture_client(["compose_healthy_api"])
        # `record_host_heartbeat` only ever advances `last_seen_at`
        # (MAX) -- must start from the real wall clock, not the fixed
        # historical `TICK_AT`, since the local host row's own
        # `first_seen_at`/`last_seen_at` was itself bootstrapped from
        # the *real* current time by `open_repo`'s underlying
        # `open_database` call a moment ago, which would otherwise
        # already be later than `TICK_AT` and silently swallow every
        # comparison in this test.
        clock = make_advancing_clock(datetime.now(UTC))
        collector = CollectorLoop(client=client, repository=repo, clock=clock)

        collector.run_once()
        first_seen = repo.get_host_by_key("local").last_seen_at
        collector.run_once()
        second_seen = repo.get_host_by_key("local").last_seen_at

        assert second_seen > first_seen
        conn.close()


# --------------------------------------------------------------------------
# History provider wiring (Milestone 9 regression)
#
# Before Milestone 9, run_once() called discover() with no history_provider
# at all, so every real, running collector evaluated every container with
# prior_observations=() forever -- restart-loop / recent-restart detection
# could never actually fire outside of discover()'s own direct unit tests.
# This proves the fix at the level a real deployment experiences it: a
# container whose restart_count genuinely climbs across real ticks, using
# nothing but CollectorLoop.run_once() and the repository it already owns.
# --------------------------------------------------------------------------


class TestHistoryProviderWiring:
    def test_rising_restart_count_across_real_ticks_is_detected_as_restart_loop(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        base_attrs = load_fixture("compose_healthy_api")
        container_id = base_attrs["Id"]

        # 4 ticks' worth of RestartCount readings: 0, 1, 2, 3 -- a container
        # crash-looping in real time. Delta from the first reading (0) to
        # the last (3) meets the default restart_loop_threshold of 3.
        #
        # RestartCount is advanced in `list()`, not `get()` -- `list()` is
        # called exactly once per tick (discovery's own first call), while
        # `get()` is now called *twice* per tick since Milestone 10
        # (once by discovery's inspect_container, once by evidence
        # collection's get_logs) -- advancing a shared counter in `get()`
        # would double-advance it per tick.
        restart_counts = iter([0, 1, 2, 3])
        current_restart_count = {"value": 0}

        class _FakeContainerHandle:
            def __init__(self, id: str, attrs: dict):
                self.id = id
                self.attrs = attrs

            def logs(self, **kwargs):
                return b""  # no logs -- this test is about restart-loop detection, not evidence

        class _CountingContainersAPI:
            def list(self, all=True):
                current_restart_count["value"] = next(restart_counts)
                return [SimpleNamespace(id=container_id)]

            def get(self, cid):
                attrs = dict(base_attrs)
                attrs["RestartCount"] = current_restart_count["value"]
                return _FakeContainerHandle(cid, attrs)

        class _CountingSDKClient:
            def __init__(self):
                self.containers = _CountingContainersAPI()

        client = DockerClient(client=_CountingSDKClient())
        clock = make_advancing_clock(TICK_AT, step_seconds=5.0)
        collector = CollectorLoop(client=client, repository=repo, clock=clock, sleep=lambda seconds: None)

        statuses = []
        for _ in range(4):
            result = collector.run_once()
            assert result.success is True
            statuses.append(repo.get_latest_observation(container_id).derived_status)
        conn.close()

        # Tick 1 has no persisted history yet (a brand new container) --
        # prior_observations=() there is correct, not a leftover bug, so it
        # reads HEALTHY. By tick 4, the repository-backed history_provider
        # has fed the three earlier, already-persisted observations back
        # into evaluate_container_health, and the climbing restart_count
        # is correctly classified as RESTARTING. Before the Milestone 9
        # fix, every one of these four ticks would have read
        # prior_observations=() and stayed HEALTHY forever, no matter how
        # high RestartCount climbed.
        assert statuses[0] is HealthStatus.HEALTHY
        assert statuses[-1] is HealthStatus.RESTARTING


# --------------------------------------------------------------------------
# run_forever
# --------------------------------------------------------------------------


class TestRunForever:
    def test_success_cadence_sleeps_poll_interval_each_time(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        client = make_fixture_client(["compose_healthy_api"])
        sleep = _RecordingSleep()
        collector = CollectorLoop(
            client=client, repository=repo, clock=make_advancing_clock(TICK_AT), sleep=sleep
        )

        collector.run_forever(stop_after_n_ticks=3)

        assert sleep.calls == [15.0, 15.0, 15.0]
        conn.close()

    def test_failure_cadence_backs_off_exponentially(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        client = make_unavailable_client()
        sleep = _RecordingSleep()
        collector = CollectorLoop(client=client, repository=repo, clock=lambda: TICK_AT, sleep=sleep)

        collector.run_forever(stop_after_n_ticks=3)

        assert sleep.calls == [15.0, 30.0, 60.0]
        conn.close()

    def test_recovery_resets_backoff_sequence(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        client = _ScriptedDockerClient(["fail", "fail", "fail", ["compose_healthy_api"], "fail"])
        sleep = _RecordingSleep()
        collector = CollectorLoop(client=client, repository=repo, clock=lambda: TICK_AT, sleep=sleep)

        collector.run_forever(stop_after_n_ticks=5)

        assert sleep.calls == [15.0, 30.0, 60.0, 15.0, 15.0]
        conn.close()

    def test_stop_predicate_halts_before_any_tick(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        client = make_fixture_client([])
        sleep = _RecordingSleep()
        collector = CollectorLoop(client=client, repository=repo, clock=lambda: TICK_AT, sleep=sleep)

        collector.run_forever(stop=lambda: True)

        assert sleep.calls == []
        assert repo.get_collector_state().last_tick_at is None
        conn.close()

    def test_unexpected_exception_in_run_once_does_not_crash_the_loop(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        client = make_fixture_client([])
        sleep = _RecordingSleep()
        collector = CollectorLoop(client=client, repository=repo, clock=lambda: TICK_AT, sleep=sleep)

        calls = {"n": 0}
        real_run_once = collector.run_once

        def flaky_run_once():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("unexpected bug, not one of the three named failure categories")
            return real_run_once()

        collector.run_once = flaky_run_once  # type: ignore[method-assign]

        collector.run_forever(stop_after_n_ticks=2)  # must not propagate the RuntimeError

        assert calls["n"] == 2
        # tick 1 (the bug) was recorded as a failure; tick 2 succeeded and reset it
        assert repo.get_collector_state().consecutive_failures == 0
        assert sleep.calls == [15.0, 15.0]  # backoff(1)=15 after the bug, then poll_interval after success
        conn.close()


# --------------------------------------------------------------------------
# Evidence collection integration (Milestone 10)
# --------------------------------------------------------------------------


def _log_ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")


class _EvidenceFakeContainer:
    def __init__(self, id: str, attrs: dict, log_lines: list = (), log_error: BaseException | None = None):
        self.id = id
        self.attrs = attrs
        self._log_lines = list(log_lines)
        self._log_error = log_error
        self.logs_calls: list[dict] = []

    def logs(self, **kwargs):
        self.logs_calls.append(kwargs)
        if self._log_error is not None:
            raise self._log_error
        since = kwargs.get("since")
        lines = self._log_lines
        if since is not None:
            lines = [(dt, text) for dt, text in lines if dt > since]
        body = "".join(f"{_log_ts(dt)} {text}\n" for dt, text in lines)
        return body.encode("utf-8")


class _EvidenceFakeContainersAPI:
    def __init__(self, by_id: dict[str, _EvidenceFakeContainer]):
        self._by_id = by_id

    def list(self, all=True):
        return [SimpleNamespace(id=cid) for cid in self._by_id]

    def get(self, cid):
        return self._by_id[cid]


class _EvidenceFakeSDKClient:
    def __init__(self, by_id: dict[str, _EvidenceFakeContainer]):
        self.containers = _EvidenceFakeContainersAPI(by_id)


def make_evidence_client(containers: dict[str, _EvidenceFakeContainer]) -> DockerClient:
    return DockerClient(client=_EvidenceFakeSDKClient(containers))


def _healthy_api_attrs(restart_count: int = 0, health: str = "healthy") -> dict:
    attrs = dict(load_fixture("compose_healthy_api"))
    attrs["RestartCount"] = restart_count
    attrs["State"] = dict(attrs["State"])
    attrs["State"]["Health"] = dict(attrs["State"]["Health"])
    attrs["State"]["Health"]["Status"] = health
    return attrs


class TestEvidenceCollectionIntegration:
    def test_evidence_is_collected_and_persisted_during_a_normal_tick(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        container_id = _healthy_api_attrs()["Id"]
        container = _EvidenceFakeContainer(
            container_id, _healthy_api_attrs(),
            log_lines=[
                (TICK_AT - timedelta(seconds=5), "connection timeout after 30s"),
                (TICK_AT - timedelta(seconds=4), "connection timeout after 30s"),
                (TICK_AT - timedelta(seconds=3), "connection timeout after 30s"),
            ],
        )
        client = make_evidence_client({container_id: container})
        collector = CollectorLoop(client=client, repository=repo, clock=lambda: TICK_AT)

        result = collector.run_once()

        assert result.success is True
        assert result.evidence_signals_created == 1
        assert result.evidence_error is None

        app = repo.get_application("cnstrct")
        signals = repo.list_log_signals_for_application(app.id, since=TICK_AT - timedelta(hours=1))
        assert len(signals) == 1
        assert signals[0].category.value == "db_connection_timeout"
        assert signals[0].count == 3
        conn.close()

    def test_no_matching_log_lines_still_advances_cursor_and_succeeds(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        container_id = _healthy_api_attrs()["Id"]
        container = _EvidenceFakeContainer(
            container_id, _healthy_api_attrs(), log_lines=[(TICK_AT - timedelta(seconds=5), "all good")]
        )
        client = make_evidence_client({container_id: container})
        collector = CollectorLoop(client=client, repository=repo, clock=lambda: TICK_AT)

        result = collector.run_once()
        assert result.success is True
        assert result.evidence_signals_created == 0
        assert result.evidence_error is None
        conn.close()

    def test_cursor_prevents_reingestion_across_ticks(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        container_id = _healthy_api_attrs()["Id"]
        container = _EvidenceFakeContainer(
            container_id, _healthy_api_attrs(),
            log_lines=[
                (TICK_AT - timedelta(seconds=10), "connection timeout"),
                (TICK_AT - timedelta(seconds=9), "connection timeout"),
            ],
        )
        client = make_evidence_client({container_id: container})
        clock = make_advancing_clock(TICK_AT, step_seconds=5.0)
        collector = CollectorLoop(client=client, repository=repo, clock=clock)

        first = collector.run_once()
        assert first.evidence_signals_created == 1
        app = repo.get_application("cnstrct")
        signal = repo.list_log_signals_for_application(app.id, since=TICK_AT - timedelta(hours=1))[0]
        assert signal.count == 2

        second = collector.run_once()
        assert second.success is True
        # no new lines were added between ticks -- the same two lines must
        # never be re-counted.
        signal_after = repo.get_log_signal(signal.id)
        assert signal_after.count == 2
        conn.close()

    def test_one_containers_log_failure_does_not_affect_another(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        healthy = _healthy_api_attrs()
        broken_attrs = dict(load_fixture("compose_postgres_nohealthcheck"))

        good = _EvidenceFakeContainer(
            healthy["Id"], healthy, log_lines=[(TICK_AT - timedelta(seconds=5), "connection timeout")]
        )
        broken = _EvidenceFakeContainer(
            broken_attrs["Id"], broken_attrs, log_error=docker.errors.DockerException("log stream broken")
        )
        client = make_evidence_client({healthy["Id"]: good, broken_attrs["Id"]: broken})
        collector = CollectorLoop(client=client, repository=repo, clock=lambda: TICK_AT)

        result = collector.run_once()

        assert result.success is True
        assert result.evidence_signals_created == 1  # the healthy container's own evidence still persisted
        assert result.evidence_error is None  # a single container's log failure isn't a subsystem failure
        conn.close()

    def test_evidence_persistence_failure_does_not_fail_the_core_tick(self, tmp_path, monkeypatch):
        conn, repo = open_repo(tmp_path)
        container_id = _healthy_api_attrs()["Id"]
        container = _EvidenceFakeContainer(
            container_id, _healthy_api_attrs(),
            log_lines=[(TICK_AT - timedelta(seconds=5), "connection timeout")],
        )
        client = make_evidence_client({container_id: container})
        collector = CollectorLoop(client=client, repository=repo, clock=lambda: TICK_AT)

        def boom(*args, **kwargs):
            raise PersistenceError("evidence db write failed")

        monkeypatch.setattr(repo, "insert_log_signal", boom)

        result = collector.run_once()

        assert result.success is True  # core monitoring is unaffected
        assert result.evidence_signals_created == 0
        assert result.evidence_error is not None
        state = repo.get_collector_state()
        assert state.consecutive_evidence_failures == 1
        assert state.consecutive_failures == 0  # core heartbeat untouched
        conn.close()

    def test_docker_fact_evidence_from_restart_count_increase(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        container_id = _healthy_api_attrs()["Id"]
        clock = make_advancing_clock(TICK_AT, step_seconds=5.0)

        client1 = make_evidence_client(
            {container_id: _EvidenceFakeContainer(container_id, _healthy_api_attrs(restart_count=0))}
        )
        collector = CollectorLoop(client=client1, repository=repo, clock=clock)
        collector.run_once()  # baseline: restart_count=0, no prior observation -> no evidence

        collector._docker_client = make_evidence_client(  # simulate the daemon reporting a real restart
            {container_id: _EvidenceFakeContainer(container_id, _healthy_api_attrs(restart_count=2))}
        )
        result = collector.run_once()

        app = repo.get_application("cnstrct")
        signals = repo.list_log_signals_for_application(app.id, since=TICK_AT - timedelta(hours=1))
        restart_signals = [s for s in signals if s.category.value == "container_restart"]
        assert len(restart_signals) == 1
        assert restart_signals[0].source_type == "docker_fact"
        assert result.evidence_signals_created >= 1
        conn.close()

    def test_docker_fact_evidence_from_unhealthy_docker_health(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        container_id = _healthy_api_attrs()["Id"]
        client = make_evidence_client(
            {container_id: _EvidenceFakeContainer(container_id, _healthy_api_attrs(health="unhealthy"))}
        )
        collector = CollectorLoop(client=client, repository=repo, clock=lambda: TICK_AT)
        collector.run_once()

        app = repo.get_application("cnstrct")
        signals = repo.list_log_signals_for_application(app.id, since=TICK_AT - timedelta(hours=1))
        assert any(s.category.value == "container_unhealthy" for s in signals)
        conn.close()

    def test_evidence_linked_to_incident_opened_in_the_same_tick(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        stopped_attrs = dict(load_fixture("stopped_error"))  # a real STOPPED-shaped fixture
        container_id = stopped_attrs["Id"]
        container = _EvidenceFakeContainer(
            container_id, stopped_attrs,
            log_lines=[(TICK_AT - timedelta(seconds=5), "connection timeout after 30s")],
        )
        client = make_evidence_client({container_id: container})
        collector = CollectorLoop(client=client, repository=repo, clock=lambda: TICK_AT)

        result = collector.run_once()
        assert result.success is True
        assert result.incidents_opened == 1
        assert result.evidence_associations >= 1

        app_key = next(iter(repo.list_applications())).key
        app = repo.get_application(app_key)
        open_incident = repo.get_open_incident(failure_signature=f"application:{app_key}")
        assert open_incident is not None
        linked = repo.list_evidence_for_incident(open_incident.id)
        assert len(linked) >= 1
        conn.close()

    def test_collect_evidence_false_disables_the_whole_subsystem(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        container_id = _healthy_api_attrs()["Id"]
        container = _EvidenceFakeContainer(
            container_id, _healthy_api_attrs(),
            log_lines=[(TICK_AT - timedelta(seconds=5), "connection timeout")],
        )
        client = make_evidence_client({container_id: container})
        config = CollectorConfig(collect_evidence=False)
        collector = CollectorLoop(client=client, repository=repo, config=config, clock=lambda: TICK_AT)

        result = collector.run_once()

        assert result.success is True
        assert result.evidence_signals_created == 0
        assert container.logs_calls == []  # get_logs() was never even called
        state = repo.get_collector_state()
        assert state.last_evidence_success_at is None
        conn.close()

    def test_signals_per_tick_cap_defers_remaining_containers_to_a_later_tick(self, tmp_path):
        conn, repo = open_repo(tmp_path)
        healthy = _healthy_api_attrs()
        postgres_attrs = dict(load_fixture("compose_postgres_nohealthcheck"))

        c1 = _EvidenceFakeContainer(
            healthy["Id"], healthy, log_lines=[(TICK_AT - timedelta(seconds=5), "connection timeout")]
        )
        c2 = _EvidenceFakeContainer(
            postgres_attrs["Id"], postgres_attrs, log_lines=[(TICK_AT - timedelta(seconds=5), "out of memory")]
        )
        client = make_evidence_client({healthy["Id"]: c1, postgres_attrs["Id"]: c2})
        limits = EvidenceCollectionLimits(max_signals_per_tick=1)
        config = CollectorConfig(evidence_limits=limits)
        collector = CollectorLoop(client=client, repository=repo, config=config, clock=lambda: TICK_AT)

        result = collector.run_once()

        assert result.success is True
        assert result.evidence_signals_created == 1  # capped -- only one container's evidence made it
        # the un-processed container's cursor must be untouched, so its
        # evidence is collected (not lost) on a later tick, not skipped
        # forever.
        uncapped_container_id = c2.id if c1.logs_calls else c1.id
        uncapped_row = repo.get_container_by_docker_id(uncapped_container_id)
        if uncapped_row is not None:
            assert repo.get_log_cursor(uncapped_row.id) is None
        conn.close()


# --------------------------------------------------------------------------
# Architecture guards
# --------------------------------------------------------------------------

FORBIDDEN_IMPORT_ROOTS = {
    "anthropic",
    "openai",
    "langgraph",
    "fastapi",
    "sqlalchemy",
    "prometheus_client",
}


def _imported_roots(source: str) -> set[str]:
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


import argus.run_collector as run_collector_module  # noqa: E402  (after fixtures, for guard tests)


class TestArchitectureGuard:
    def test_collector_package_has_no_ai_or_web_or_metrics_imports(self):
        source = inspect.getsource(loop_module) + inspect.getsource(run_collector_module)
        found = _imported_roots(source) & FORBIDDEN_IMPORT_ROOTS
        assert not found, f"argus.collector imports forbidden module(s): {found}"

    def test_collector_package_imports_only_expected_layers(self):
        source = inspect.getsource(loop_module) + inspect.getsource(run_collector_module)
        roots = _imported_roots(source)
        assert "argus" in roots
        assert "datetime" in roots
        assert "logging" in roots

    def test_collector_loop_never_imports_docker_sdk_directly(self):
        """All Docker access must flow through argus.collectors.docker_client;
        loop.py itself must not `import docker`."""
        source = inspect.getsource(loop_module)
        roots = _imported_roots(source)
        assert "docker" not in roots

    def test_collector_loop_never_reaches_into_docker_client_internals(self):
        source = inspect.getsource(loop_module) + inspect.getsource(run_collector_module)
        assert "._client" not in source

    def test_no_mutating_docker_calls_anywhere_in_new_code(self):
        mutating_patterns = (
            ".start(", ".stop(", ".restart(", ".kill(", ".remove(", ".exec_run(",
            ".pause(", ".unpause(", ".rename(", ".update(", ".prune(", ".build(",
            ".pull(", ".push(", ".create(", ".run(", ".commit(",
        )
        source = inspect.getsource(loop_module) + inspect.getsource(run_collector_module)
        found = [p for p in mutating_patterns if p in source]
        assert not found, f"collector code contains mutating Docker call(s): {found}"
