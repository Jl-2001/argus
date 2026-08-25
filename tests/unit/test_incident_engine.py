"""Tests for argus.incidents.engine.

Each test drives process_transitions_and_incidents() directly against a
real (temporary, file-backed) database through several synthetic
"ticks" -- there is no mocking of the engine itself, and no real Docker
or collector loop involved; that integration is covered separately in
test_collector_loop.py's existing tests (which now exercise this
engine as part of a full tick) and by this file's own restart-safety
test.
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from argus.domain.models import (
    Application,
    Container,
    DockerHealth,
    DockerState,
    HealthStatus,
    Observation,
    Service,
)
from argus.incidents import engine as engine_module
from argus.incidents.engine import (
    IncidentProcessingError,
    IncidentProcessingResult,
    incident_severity_rank,
    process_transitions_and_incidents,
)
from argus.store.database import DatabaseOpenError, SchemaError, open_database
from argus.store.repository import Repository

UTC = timezone.utc
T0 = datetime(2026, 8, 21, 10, 0, 0, tzinfo=UTC)


def tick_times(n: int, step_seconds: float = 15.0):
    return [T0 + timedelta(seconds=step_seconds * i) for i in range(n)]


# --------------------------------------------------------------------------
# Test harness: seed identity rows, then drive a sequence of statuses
# --------------------------------------------------------------------------


def seed_identity(
    repository: Repository,
    *,
    key: str = "cnstrct",
    name: str = "CNSTRCT",
    compose_service: str = "api",
    container_id: str = "aaa",
    observed_at: datetime = T0,
):
    repository.upsert_application(key=key, name=name, is_standalone=False, observed_at=observed_at)
    app = repository.get_application(key)
    repository.upsert_service(
        application_id=app.id, compose_service=compose_service, name=compose_service, observed_at=observed_at
    )
    service = repository.get_service_by_key(application_id=app.id, compose_service=compose_service)
    repository.upsert_container(
        service_id=service.id,
        container_id=container_id,
        name=f"{key}-{compose_service}-1",
        first_seen_at=observed_at,
        last_seen_at=observed_at,
    )
    return app, service


def make_single_service_application(
    *, key: str, name: str, compose_service: str, container_id: str, status: HealthStatus
) -> Application:
    container = Container(
        container_id=container_id,
        name=f"{key}-{compose_service}-1",
        image="argus-fixtures/app:1",
        compose_project=key,
        compose_service=compose_service,
        first_seen_at=T0,
        last_seen_at=T0,
    )
    service = Service(
        application_key=key, compose_service=compose_service, containers=(container,), derived_status=status
    )
    return Application(key=key, name=name, is_standalone=False, services=(service,), derived_status=status)


def make_observation(
    container: Container, *, observed_at: datetime, derived_status: HealthStatus
) -> Observation:
    """A minimal, real Observation to persist directly -- used only by the
    occurred_at-backfill tests, which need actual observation history for
    _find_true_occurred_at to search."""

    return Observation(
        container_ref=container,
        observed_at=observed_at,
        docker_state=DockerState.RUNNING,
        docker_health=DockerHealth.HEALTHY if derived_status is HealthStatus.HEALTHY else DockerHealth.UNHEALTHY,
        restart_count=0,
        exit_code=None,
        started_at=None,
        finished_at=None,
        ports=(),
        labels={},
        derived_status=derived_status,
        derived_detail=None,
    )


def run_sequence(
    repository: Repository,
    statuses: list[HealthStatus],
    *,
    key: str = "cnstrct",
    name: str = "CNSTRCT",
    compose_service: str = "api",
    container_id: str = "aaa",
) -> list[IncidentProcessingResult]:
    """Seed identity once, then feed each status as its own tick."""

    seed_identity(repository, key=key, name=name, compose_service=compose_service, container_id=container_id)
    results = []
    for occurred_at, status in zip(tick_times(len(statuses)), statuses):
        application = make_single_service_application(
            key=key, name=name, compose_service=compose_service, container_id=container_id, status=status
        )
        result = process_transitions_and_incidents(
            repository=repository,
            applications=[application],
            container_statuses={container_id: status},
            occurred_at=occurred_at,
        )
        results.append(result)
    return results


def all_incidents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM incidents ORDER BY id").fetchall()


def all_transitions(conn: sqlite3.Connection, scope: str | None = None) -> list[sqlite3.Row]:
    if scope is None:
        return conn.execute("SELECT * FROM health_transitions ORDER BY id").fetchall()
    return conn.execute(
        "SELECT * FROM health_transitions WHERE scope = ? ORDER BY id", (scope,)
    ).fetchall()


# --------------------------------------------------------------------------
# Schema migration v2 -> v3
# --------------------------------------------------------------------------


class TestSchemaMigrationV2ToV3:
    def test_fresh_database_has_health_transitions_and_incidents(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8  # SCHEMA_VERSION moved to 8 in Milestone 16 (multi-host agents)
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {"health_transitions", "incidents"} <= tables
        conn.close()

    def test_existing_v2_database_migrates_without_losing_data(self, tmp_path):
        db_path = tmp_path / "a.db"

        # A genuine pre-Milestone-6 (Milestone 5) database: v1 tables plus
        # collector_state, no health_transitions/incidents, user_version=2.
        v2_conn = sqlite3.connect(str(db_path))
        v2_conn.executescript(
            """
            CREATE TABLE applications (
                id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                is_standalone INTEGER NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
            );
            CREATE TABLE collector_state (
                id INTEGER PRIMARY KEY CHECK (id = 1), last_tick_at TEXT, last_success_at TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0, last_error TEXT
            );
            """
        )
        v2_conn.execute(
            "INSERT INTO applications (key, name, is_standalone, first_seen_at, last_seen_at) "
            "VALUES ('cnstrct','CNSTRCT',0,?,?)",
            (T0.isoformat(), T0.isoformat()),
        )
        v2_conn.execute("PRAGMA user_version = 2")
        v2_conn.commit()
        v2_conn.close()

        conn = open_database(db_path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8  # SCHEMA_VERSION moved to 8 in Milestone 16 (multi-host agents)
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {"health_transitions", "incidents"} <= tables

        row = conn.execute("SELECT * FROM applications WHERE key = 'cnstrct'").fetchone()
        assert row["name"] == "CNSTRCT"
        conn.close()

    def test_existing_v3_database_opens_safely(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        Repository(conn).upsert_application(
            key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T0
        )
        conn.close()

        conn2 = open_database(db_path)
        assert conn2.execute("PRAGMA user_version").fetchone()[0] == 8  # SCHEMA_VERSION moved to 8 in Milestone 16 (multi-host agents)
        assert Repository(conn2).get_application("cnstrct") is not None
        conn2.close()

    def test_unsupported_future_version_raises(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        conn.execute("PRAGMA user_version = 99")
        conn.close()
        with pytest.raises(SchemaError):
            open_database(db_path)


# --------------------------------------------------------------------------
# Container / service transition recording
# --------------------------------------------------------------------------


class TestContainerAndServiceTransitions:
    def test_repeated_identical_status_produces_no_duplicate_rows(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        run_sequence(repo, [HealthStatus.HEALTHY, HealthStatus.HEALTHY, HealthStatus.HEALTHY])

        container_rows = all_transitions(conn, scope="container")
        service_rows = all_transitions(conn, scope="service")
        assert len(container_rows) == 1  # NULL -> HEALTHY, once
        assert len(service_rows) == 1
        assert container_rows[0]["from_status"] is None
        assert container_rows[0]["to_status"] == "HEALTHY"
        conn.close()

    def test_container_and_service_three_row_sequence(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        run_sequence(
            repo,
            [HealthStatus.HEALTHY, HealthStatus.HEALTHY, HealthStatus.DEGRADED, HealthStatus.HEALTHY],
        )

        container_rows = all_transitions(conn, scope="container")
        service_rows = all_transitions(conn, scope="service")

        assert [(r["from_status"], r["to_status"]) for r in container_rows] == [
            (None, "HEALTHY"),
            ("HEALTHY", "DEGRADED"),
            ("DEGRADED", "HEALTHY"),
        ]
        # v0.1: service mirrors its single container's status exactly
        assert [(r["from_status"], r["to_status"]) for r in service_rows] == [
            (None, "HEALTHY"),
            ("HEALTHY", "DEGRADED"),
            ("DEGRADED", "HEALTHY"),
        ]
        conn.close()


# --------------------------------------------------------------------------
# Application rollup transition (multi-service)
# --------------------------------------------------------------------------


class TestApplicationRollupTransition:
    def test_one_child_service_change_produces_one_app_incident(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)

        def make_app(api_status, postgres_status, redis_status, app_status):
            def svc(name, status, cid):
                container = Container(
                    container_id=cid, name=f"cnstrct-{name}-1", image="img:1",
                    compose_project="cnstrct", compose_service=name, first_seen_at=T0, last_seen_at=T0,
                )
                return Service(
                    application_key="cnstrct", compose_service=name, containers=(container,),
                    derived_status=status,
                )
            return Application(
                key="cnstrct", name="CNSTRCT", is_standalone=False,
                services=(
                    svc("api", api_status, "api-1"),
                    svc("postgres", postgres_status, "pg-1"),
                    svc("redis", redis_status, "redis-1"),
                ),
                derived_status=app_status,
            )

        # seed identity for all three services/containers
        repo.upsert_application(key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T0)
        app_record = repo.get_application("cnstrct")
        for name, cid in (("api", "api-1"), ("postgres", "pg-1"), ("redis", "redis-1")):
            repo.upsert_service(application_id=app_record.id, compose_service=name, name=name, observed_at=T0)
            service_record = repo.get_service_by_key(application_id=app_record.id, compose_service=name)
            repo.upsert_container(
                service_id=service_record.id, container_id=cid, name=f"cnstrct-{name}-1",
                first_seen_at=T0, last_seen_at=T0,
            )

        times = tick_times(3)
        r1 = process_transitions_and_incidents(
            repository=repo,
            applications=[make_app(HealthStatus.HEALTHY, HealthStatus.HEALTHY, HealthStatus.HEALTHY, HealthStatus.HEALTHY)],
            container_statuses={"api-1": HealthStatus.HEALTHY, "pg-1": HealthStatus.HEALTHY, "redis-1": HealthStatus.HEALTHY},
            occurred_at=times[0],
        )
        r2 = process_transitions_and_incidents(
            repository=repo,
            applications=[make_app(HealthStatus.HEALTHY, HealthStatus.UNHEALTHY, HealthStatus.HEALTHY, HealthStatus.UNHEALTHY)],
            container_statuses={"api-1": HealthStatus.HEALTHY, "pg-1": HealthStatus.UNHEALTHY, "redis-1": HealthStatus.HEALTHY},
            occurred_at=times[1],
        )
        r3 = process_transitions_and_incidents(
            repository=repo,
            applications=[make_app(HealthStatus.HEALTHY, HealthStatus.HEALTHY, HealthStatus.HEALTHY, HealthStatus.HEALTHY)],
            container_statuses={"api-1": HealthStatus.HEALTHY, "pg-1": HealthStatus.HEALTHY, "redis-1": HealthStatus.HEALTHY},
            occurred_at=times[2],
        )

        assert r1.incidents_opened == 0
        assert r2.incidents_opened == 1
        assert r3.incidents_resolved == 1

        incidents = all_incidents(conn)
        assert len(incidents) == 1
        assert incidents[0]["status"] == "resolved"
        assert incidents[0]["failure_signature"] == "application:cnstrct"
        conn.close()


# --------------------------------------------------------------------------
# Required core incident sequences
# --------------------------------------------------------------------------


class TestIncidentLifecycleSequences:
    def test_single_incident_no_duplicates(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        run_sequence(
            repo,
            [
                HealthStatus.HEALTHY,
                HealthStatus.HEALTHY,
                HealthStatus.UNHEALTHY,
                HealthStatus.UNHEALTHY,
                HealthStatus.UNHEALTHY,
                HealthStatus.HEALTHY,
            ],
        )

        app_transitions = all_transitions(conn, scope="application")
        assert [(r["from_status"], r["to_status"]) for r in app_transitions] == [
            (None, "HEALTHY"),
            ("HEALTHY", "UNHEALTHY"),
            ("UNHEALTHY", "HEALTHY"),
        ]

        incidents = all_incidents(conn)
        assert len(incidents) == 1
        assert incidents[0]["status"] == "resolved"
        conn.close()

    def test_flapping_escalation_worst_status_and_single_resolution(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        run_sequence(
            repo,
            [
                HealthStatus.HEALTHY,
                HealthStatus.DEGRADED,
                HealthStatus.UNHEALTHY,
                HealthStatus.DEGRADED,
                HealthStatus.UNHEALTHY,
                HealthStatus.HEALTHY,
            ],
        )

        incidents = all_incidents(conn)
        assert len(incidents) == 1
        assert incidents[0]["worst_status"] == "UNHEALTHY"
        assert incidents[0]["status"] == "resolved"
        conn.close()

    def test_multi_episode_produces_two_resolved_incidents(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        run_sequence(
            repo,
            [
                HealthStatus.HEALTHY,
                HealthStatus.UNHEALTHY,
                HealthStatus.HEALTHY,
                HealthStatus.UNHEALTHY,
                HealthStatus.HEALTHY,
            ],
        )

        incidents = all_incidents(conn)
        assert len(incidents) == 2
        assert all(i["status"] == "resolved" for i in incidents)
        conn.close()

    def test_unknown_opens_and_keeps_open_through_unhealthy(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        run_sequence(
            repo,
            [
                HealthStatus.HEALTHY,
                HealthStatus.UNKNOWN,
                HealthStatus.UNKNOWN,
                HealthStatus.UNHEALTHY,
                HealthStatus.HEALTHY,
            ],
        )

        incidents = all_incidents(conn)
        assert len(incidents) == 1
        assert incidents[0]["opening_status"] == "UNKNOWN"
        assert incidents[0]["worst_status"] == "UNHEALTHY"
        assert incidents[0]["status"] == "resolved"
        conn.close()

    def test_stopped_opens_once_and_resolves_once(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        run_sequence(
            repo,
            [HealthStatus.HEALTHY, HealthStatus.STOPPED, HealthStatus.STOPPED, HealthStatus.HEALTHY],
        )

        incidents = all_incidents(conn)
        assert len(incidents) == 1
        assert incidents[0]["opening_status"] == "STOPPED"
        assert incidents[0]["status"] == "resolved"
        conn.close()

    def test_initial_bad_state_opens_incident(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        results = run_sequence(repo, [HealthStatus.UNHEALTHY])

        assert results[0].incidents_opened == 1
        app_transitions = all_transitions(conn, scope="application")
        assert app_transitions[0]["from_status"] is None
        assert app_transitions[0]["to_status"] == "UNHEALTHY"

        incidents = all_incidents(conn)
        assert len(incidents) == 1
        assert incidents[0]["status"] == "open"
        conn.close()

    def test_initial_healthy_state_opens_no_incident(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        results = run_sequence(repo, [HealthStatus.HEALTHY])

        assert results[0].incidents_opened == 0
        assert results[0].incidents_resolved == 0
        assert all_incidents(conn) == []
        conn.close()

    def test_resolution_without_open_incident_is_a_no_op(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        # HEALTHY on the very first-ever tick: a transition is recorded
        # (NULL -> HEALTHY) but there is nothing to resolve.
        results = run_sequence(repo, [HealthStatus.HEALTHY])

        assert results[0].incidents_resolved == 0
        assert all_incidents(conn) == []
        conn.close()


# --------------------------------------------------------------------------
# Milestone 15 -- IncidentProcessingResult's additive detail fields
# (transitions/opened_incidents/updated_incidents/resolved_incidents),
# the real data argus.realtime.emitter turns into events. Unlike
# test_realtime_emitter.py (which hand-builds an IncidentProcessingResult
# to test the emitter in isolation), these tests drive the real engine
# end to end and check *it* populates the tuples correctly.
# --------------------------------------------------------------------------


class TestRealtimeDetailFields:
    def test_every_committed_transition_is_recorded_with_its_real_identifiers(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        results = run_sequence(repo, [HealthStatus.HEALTHY, HealthStatus.UNHEALTHY])

        # tick 0: application/service/container all go None -> HEALTHY (3 transitions)
        assert len(results[0].transitions) == 3
        assert {t.scope for t in results[0].transitions} == {"application", "service", "container"}
        for t in results[0].transitions:
            assert t.from_status is None
            assert t.to_status is HealthStatus.HEALTHY
            assert t.application_key == "cnstrct"
            assert isinstance(t.transition_id, int)

        # tick 1: HEALTHY -> UNHEALTHY (3 more transitions, this time with a real from_status)
        assert len(results[1].transitions) == 3
        for t in results[1].transitions:
            assert t.from_status is HealthStatus.HEALTHY
            assert t.to_status is HealthStatus.UNHEALTHY
        conn.close()

    def test_an_unchanged_status_records_no_transition(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        results = run_sequence(repo, [HealthStatus.HEALTHY, HealthStatus.HEALTHY])
        assert results[1].transitions == ()
        conn.close()

    def test_full_escalation_deescalation_resolve_sequence(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        results = run_sequence(
            repo,
            [
                HealthStatus.HEALTHY,      # 0: no incident
                HealthStatus.DEGRADED,     # 1: opens
                HealthStatus.UNHEALTHY,    # 2: escalates (worst DEGRADED -> UNHEALTHY)
                HealthStatus.DEGRADED,     # 3: real transition, NOT an escalation (worst stays UNHEALTHY)
                HealthStatus.UNHEALTHY,    # 4: real transition, NOT an escalation (worst already UNHEALTHY)
                HealthStatus.HEALTHY,      # 5: resolves
            ],
        )

        assert results[0].opened_incidents == () and results[0].updated_incidents == () and results[0].resolved_incidents == ()

        assert len(results[1].opened_incidents) == 1
        assert results[1].opened_incidents[0].opening_status is HealthStatus.DEGRADED
        assert results[1].opened_incidents[0].application_key == "cnstrct"
        assert results[1].updated_incidents == ()

        assert results[2].opened_incidents == ()
        assert len(results[2].updated_incidents) == 1
        assert results[2].updated_incidents[0].worst_status is HealthStatus.UNHEALTHY

        # -- the two "no duplicate update" cases: a real, committed
        # transition happened, but since it wasn't worse than the
        # incident's already-recorded worst_status, no updated_incidents
        # entry is appended (this is the "no duplicate/no every-poll
        # update" guarantee the realtime layer relies on) --
        assert results[3].transitions != ()
        assert results[3].updated_incidents == ()
        assert results[4].transitions != ()
        assert results[4].updated_incidents == ()

        assert results[5].opened_incidents == () and results[5].updated_incidents == ()
        assert len(results[5].resolved_incidents) == 1
        assert results[5].resolved_incidents[0].application_key == "cnstrct"

        incident_ids = {
            results[1].opened_incidents[0].incident_id,
            results[2].updated_incidents[0].incident_id,
            results[5].resolved_incidents[0].incident_id,
        }
        assert len(incident_ids) == 1  # all refer to the exact same incident
        conn.close()

    def test_first_ever_null_to_healthy_transition_opens_no_incident(self, tmp_path):
        """HEALTHY never opens an incident (see _update_incident_lifecycle) --
        confirms the detail-field extension didn't change that."""

        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        [result] = run_sequence(repo, [HealthStatus.HEALTHY])
        assert len(result.transitions) == 3  # application/service/container all recorded
        assert result.opened_incidents == ()
        conn.close()


# --------------------------------------------------------------------------
# DB-level dedup guarantee
# --------------------------------------------------------------------------


class TestDatabaseConstraint:
    def test_second_open_incident_with_same_signature_is_rejected_at_db_layer(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        seed_identity(repo)
        app = repo.get_application("cnstrct")

        transition_id = repo.insert_transition(
            scope="application", scope_id=app.id, from_status=None,
            to_status=HealthStatus.UNHEALTHY, occurred_at=T0,
        )
        repo.open_incident(
            scope_id=app.id, failure_signature="application:cnstrct", opened_at=T0,
            opening_status=HealthStatus.UNHEALTHY, opening_transition_id=transition_id,
        )

        # Bypass application logic entirely and try to insert a second open
        # row with the same signature directly -- the partial unique index
        # must reject it regardless.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO incidents (scope, scope_id, failure_signature, opened_at, status, "
                "opening_status, worst_status, opening_transition_id) "
                "VALUES ('application', ?, 'application:cnstrct', ?, 'open', 'UNHEALTHY', 'UNHEALTHY', ?)",
                (app.id, T0.isoformat(), transition_id),
            )
        conn.close()

    def test_repository_open_incident_wraps_constraint_violation(self, tmp_path):
        from argus.store.database import DuplicateIncidentError

        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        seed_identity(repo)
        app = repo.get_application("cnstrct")
        transition_id = repo.insert_transition(
            scope="application", scope_id=app.id, from_status=None,
            to_status=HealthStatus.UNHEALTHY, occurred_at=T0,
        )
        repo.open_incident(
            scope_id=app.id, failure_signature="application:cnstrct", opened_at=T0,
            opening_status=HealthStatus.UNHEALTHY, opening_transition_id=transition_id,
        )

        with pytest.raises(DuplicateIncidentError):
            repo.open_incident(
                scope_id=app.id, failure_signature="application:cnstrct", opened_at=T0,
                opening_status=HealthStatus.UNHEALTHY, opening_transition_id=transition_id,
            )
        conn.close()


# --------------------------------------------------------------------------
# Restart safety
# --------------------------------------------------------------------------


class TestRestartSimulation:
    def test_restarting_against_the_same_db_does_not_duplicate(self, tmp_path):
        db_path = tmp_path / "a.db"

        conn1 = open_database(db_path)
        repo1 = Repository(conn1)
        seed_identity(repo1)
        app = make_single_service_application(
            key="cnstrct", name="CNSTRCT", compose_service="api", container_id="aaa",
            status=HealthStatus.UNHEALTHY,
        )
        process_transitions_and_incidents(
            repository=repo1, applications=[app],
            container_statuses={"aaa": HealthStatus.UNHEALTHY}, occurred_at=T0,
        )
        conn1.close()  # simulate the Argus process exiting

        # Fresh connection/repository -- nothing carried over in memory.
        conn2 = open_database(db_path)
        repo2 = Repository(conn2)
        result = process_transitions_and_incidents(
            repository=repo2, applications=[app],
            container_statuses={"aaa": HealthStatus.UNHEALTHY}, occurred_at=T0 + timedelta(seconds=15),
        )

        assert result.transitions_created == 0
        assert result.incidents_opened == 0
        assert len(all_transitions(conn2, scope="application")) == 1  # still just the one
        assert len(all_incidents(conn2)) == 1
        assert all_incidents(conn2)[0]["status"] == "open"
        conn2.close()


# --------------------------------------------------------------------------
# Duplicate tick safety (two identical polls)
# --------------------------------------------------------------------------


class TestDuplicateTickSafety:
    def test_two_identical_unhealthy_ticks_produce_one_transition_one_incident(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        results = run_sequence(repo, [HealthStatus.UNHEALTHY, HealthStatus.UNHEALTHY])

        assert results[0].transitions_created == 3  # container+service+application, first tick
        assert results[1].transitions_created == 0  # nothing changed

        assert len(all_transitions(conn, scope="application")) == 1
        assert len(all_incidents(conn)) == 1
        conn.close()


# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------


class TestFailureHandling:
    def test_missing_container_status_raises_and_rolls_back(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        seed_identity(repo)
        app = make_single_service_application(
            key="cnstrct", name="CNSTRCT", compose_service="api", container_id="aaa",
            status=HealthStatus.HEALTHY,
        )

        with pytest.raises(IncidentProcessingError):
            process_transitions_and_incidents(
                repository=repo,
                applications=[app],
                container_statuses={},  # missing entry for "aaa"
                occurred_at=T0,
            )

        # the transaction rolled back -- no partial transition was left behind
        assert all_transitions(conn) == []
        assert all_incidents(conn) == []
        conn.close()

    def test_unknown_application_raises(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app = make_single_service_application(
            key="never-persisted", name="Ghost", compose_service="api", container_id="zzz",
            status=HealthStatus.HEALTHY,
        )
        with pytest.raises(IncidentProcessingError):
            process_transitions_and_incidents(
                repository=repo, applications=[app],
                container_statuses={"zzz": HealthStatus.HEALTHY}, occurred_at=T0,
            )
        conn.close()

    def test_incident_processing_error_is_a_persistence_error(self):
        from argus.store.database import PersistenceError

        assert issubclass(IncidentProcessingError, PersistenceError)


# --------------------------------------------------------------------------
# incident_severity_rank
# --------------------------------------------------------------------------


class TestIncidentSeverityRank:
    def test_ranking_order(self):
        assert incident_severity_rank(HealthStatus.UNHEALTHY) > incident_severity_rank(HealthStatus.STOPPED)
        assert incident_severity_rank(HealthStatus.STOPPED) > incident_severity_rank(HealthStatus.RESTARTING)
        assert incident_severity_rank(HealthStatus.RESTARTING) > incident_severity_rank(HealthStatus.DEGRADED)
        assert incident_severity_rank(HealthStatus.DEGRADED) > incident_severity_rank(HealthStatus.UNKNOWN)

    def test_healthy_has_no_rank(self):
        with pytest.raises(ValueError):
            incident_severity_rank(HealthStatus.HEALTHY)


# --------------------------------------------------------------------------
# Milestone 7 hardening check: occurred_at backfills to the observation that
# actually proves a status change, rather than to whichever later tick
# happens to notice it.
# --------------------------------------------------------------------------


class TestOccurredAtBackfillsFromObservationHistory:
    def test_missed_transition_tick_backfills_to_the_proving_observation(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        seed_identity(repo)
        container_row = repo.get_container_by_docker_id("aaa")

        container = Container(
            container_id="aaa", name="cnstrct-api-1", image="argus-fixtures/app:1",
            compose_project="cnstrct", compose_service="api", first_seen_at=T0, last_seen_at=T0,
        )

        t0, t1, t2 = T0, T0 + timedelta(seconds=15), T0 + timedelta(seconds=30)

        # T0: the initial HEALTHY transition, with a matching persisted
        # Observation to bound later history searches against.
        repo.insert_observation(
            container_row_id=container_row.id,
            observation=make_observation(container, observed_at=t0, derived_status=HealthStatus.HEALTHY),
        )
        process_transitions_and_incidents(
            repository=repo,
            applications=[make_single_service_application(
                key="cnstrct", name="CNSTRCT", compose_service="api", container_id="aaa",
                status=HealthStatus.HEALTHY,
            )],
            container_statuses={"aaa": HealthStatus.HEALTHY},
            occurred_at=t0,
        )

        # T1: the real moment the container became UNHEALTHY. Its
        # Observation is persisted (as persist_discovery always does,
        # regardless of what happens next) but transition processing for
        # this tick is simulated as having failed -- i.e. it is simply
        # never invoked for t1 at all.
        repo.insert_observation(
            container_row_id=container_row.id,
            observation=make_observation(container, observed_at=t1, derived_status=HealthStatus.UNHEALTHY),
        )

        # T2: a later tick where transition processing finally succeeds,
        # still observing UNHEALTHY.
        process_transitions_and_incidents(
            repository=repo,
            applications=[make_single_service_application(
                key="cnstrct", name="CNSTRCT", compose_service="api", container_id="aaa",
                status=HealthStatus.UNHEALTHY,
            )],
            container_statuses={"aaa": HealthStatus.UNHEALTHY},
            occurred_at=t2,
        )

        for scope in ("container", "service", "application"):
            rows = all_transitions(conn, scope=scope)
            assert len(rows) == 2, f"expected exactly 2 {scope} transitions"
            healthy_to_unhealthy = rows[-1]
            assert healthy_to_unhealthy["to_status"] == "UNHEALTHY"
            assert healthy_to_unhealthy["occurred_at"] == t1.isoformat(), (
                f"{scope} transition occurred_at should backfill to t1 (when the observation "
                f"proving UNHEALTHY was persisted), not t2 (when processing finally noticed)"
            )

        conn.close()

    def test_no_observation_history_falls_back_to_tick_timestamp(self, tmp_path):
        """The ordinary case for every existing Milestone 6 test: no real
        Observation rows exist (synthetic direct-engine calls), so the
        correction has nothing to find and behaves exactly as before --
        occurred_at is the tick's own timestamp."""

        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        results = run_sequence(repo, [HealthStatus.HEALTHY, HealthStatus.UNHEALTHY])

        app_transitions = all_transitions(conn, scope="application")
        assert app_transitions[-1]["occurred_at"] == tick_times(2)[1].isoformat()
        conn.close()


# --------------------------------------------------------------------------
# Architecture guard
# --------------------------------------------------------------------------

FORBIDDEN_IMPORT_ROOTS = {
    "docker",
    "anthropic",
    "openai",
    "langgraph",
    "fastapi",
    "requests",
    "httpx",
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


def _imported_dotted_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


class TestArchitectureGuard:
    def test_incidents_engine_has_no_forbidden_imports(self):
        source = inspect.getsource(engine_module)
        found = _imported_roots(source) & FORBIDDEN_IMPORT_ROOTS
        assert not found, f"argus.incidents.engine imports forbidden module(s): {found}"

    def test_incidents_engine_may_import_domain_and_store(self):
        source = inspect.getsource(engine_module)
        roots = _imported_roots(source)
        assert "argus" in roots

    def test_incidents_engine_does_not_import_collectors(self):
        source = inspect.getsource(engine_module)
        modules = _imported_dotted_modules(source)
        assert not any(m.startswith("argus.collectors") for m in modules)
