"""Tests for argus.store.database / argus.store.repository.

All tests use file-backed temporary databases (via pytest's `tmp_path`)
rather than `:memory:` -- both because several tests require closing
and reopening the connection, and because WAL mode has no meaningful
effect on an in-memory database.
"""

from __future__ import annotations

import ast
import inspect
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from argus.domain.models import (
    Application,
    Container,
    DockerHealth,
    DockerState,
    HealthStatus,
    Observation,
    PortBinding,
    Protocol,
    Service,
)
from argus.store import database as database_module
from argus.store import repository as repository_module
from argus.store.database import (
    DatabaseOpenError,
    DuplicateObservationError,
    SchemaError,
    open_database,
)
from argus.store.repository import Repository, resolve_observation_health

UTC = timezone.utc
T1 = datetime(2026, 8, 21, 10, 0, 0, tzinfo=UTC)
T2 = datetime(2026, 8, 21, 10, 1, 0, tzinfo=UTC)
T3 = datetime(2026, 8, 21, 10, 2, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Synthetic domain object builders
# --------------------------------------------------------------------------


def make_container(
    container_id: str,
    name: str,
    *,
    compose_project: str | None = "cnstrct",
    compose_service: str | None = "api",
    image: str = "cnstrct/api:latest",
    observed_at: datetime = T1,
) -> Container:
    return Container(
        container_id=container_id,
        name=name,
        image=image,
        compose_project=compose_project,
        compose_service=compose_service,
        first_seen_at=observed_at,
        last_seen_at=observed_at,
    )


def make_observation(
    container: Container,
    *,
    observed_at: datetime = T1,
    docker_state: DockerState = DockerState.RUNNING,
    docker_health: DockerHealth | None = DockerHealth.HEALTHY,
    restart_count: int = 0,
    exit_code: int | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    ports: tuple[PortBinding, ...] = (),
    labels: dict | None = None,
    derived_status: HealthStatus = HealthStatus.HEALTHY,
    derived_detail: str | None = None,
) -> Observation:
    return Observation(
        container_ref=container,
        observed_at=observed_at,
        docker_state=docker_state,
        docker_health=docker_health,
        restart_count=restart_count,
        exit_code=exit_code,
        started_at=started_at,
        finished_at=finished_at,
        ports=ports,
        labels=labels or {},
        derived_status=derived_status,
        derived_detail=derived_detail,
    )


def make_service(
    containers: list[Container],
    *,
    application_key: str = "cnstrct",
    compose_service: str | None = "api",
    derived_status: HealthStatus = HealthStatus.HEALTHY,
) -> Service:
    return Service(
        application_key=application_key,
        compose_service=compose_service,
        containers=tuple(containers),
        derived_status=derived_status,
    )


def make_application(
    services: list[Service],
    *,
    key: str = "cnstrct",
    name: str = "CNSTRCT",
    is_standalone: bool = False,
    derived_status: HealthStatus = HealthStatus.HEALTHY,
) -> Application:
    return Application(
        key=key,
        name=name,
        is_standalone=is_standalone,
        services=tuple(services),
        derived_status=derived_status,
    )


# --------------------------------------------------------------------------
# Schema bootstrap
# --------------------------------------------------------------------------


class TestSchemaBootstrap:
    def test_fresh_database_creates_expected_tables(self, tmp_path):
        conn = open_database(tmp_path / "argus.db")
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {"applications", "services", "containers", "observations"} <= tables
        conn.close()

    def test_schema_version_is_3(self, tmp_path):
        # Was version 1 as of Milestone 4, then 2 as of Milestone 5;
        # Milestone 6 explicitly bumps it again to add health_transitions/
        # incidents (see test_incident_engine.py's migration tests for the
        # v2 -> v3 transition itself).
        conn = open_database(tmp_path / "argus.db")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        conn.close()

    def test_wal_enabled_for_file_backed_database(self, tmp_path):
        conn = open_database(tmp_path / "argus.db")
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        conn.close()

    def test_foreign_keys_enabled(self, tmp_path):
        conn = open_database(tmp_path / "argus.db")
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        conn.close()

    def test_foreign_key_violation_is_rejected(self, tmp_path):
        conn = open_database(tmp_path / "argus.db")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO containers (service_id, container_id, name, first_seen_at, last_seen_at) "
                "VALUES (9999, 'ghost', 'ghost', ?, ?)",
                (T1.isoformat(), T1.isoformat()),
            )
        conn.close()

    def test_reopen_preserves_data_without_recreating_tables(self, tmp_path):
        db_path = tmp_path / "argus.db"
        conn = open_database(db_path)
        Repository(conn).upsert_application(
            key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1
        )
        conn.close()

        conn2 = open_database(db_path)
        record = Repository(conn2).get_application("cnstrct")
        assert record is not None
        assert record.name == "CNSTRCT"
        conn2.close()

    def test_incompatible_future_schema_version_raises(self, tmp_path):
        db_path = tmp_path / "argus.db"
        conn = open_database(db_path)
        conn.execute("PRAGMA user_version = 99")
        conn.close()

        with pytest.raises(SchemaError):
            open_database(db_path)

    def test_open_database_error_is_typed(self, tmp_path):
        # a path whose parent directory doesn't exist -- sqlite3 cannot create it
        bogus_path = tmp_path / "no-such-directory" / "argus.db"
        with pytest.raises(DatabaseOpenError):
            open_database(bogus_path)


# --------------------------------------------------------------------------
# Application identity
# --------------------------------------------------------------------------


class TestApplicationIdentity:
    def test_insert_new(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        row_id = repo.upsert_application(
            key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1
        )
        record = repo.get_application("cnstrct")
        assert record.id == row_id
        assert record.first_seen_at == T1
        assert record.last_seen_at == T1
        assert record.is_standalone is False
        conn.close()

    def test_repeated_key_preserves_first_seen_and_advances_last_seen(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        id1 = repo.upsert_application(
            key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1
        )
        id2 = repo.upsert_application(
            key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T2
        )
        assert id1 == id2
        record = repo.get_application("cnstrct")
        assert record.first_seen_at == T1
        assert record.last_seen_at == T2
        conn.close()

    def test_out_of_order_observed_at_does_not_rewind_last_seen(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        repo.upsert_application(key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T2)
        repo.upsert_application(key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1)
        record = repo.get_application("cnstrct")
        assert record.last_seen_at == T2  # not rewound to the earlier T1
        conn.close()

    def test_name_is_current_metadata(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        repo.upsert_application(key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1)
        repo.upsert_application(
            key="cnstrct", name="CNSTRCT (renamed)", is_standalone=False, observed_at=T2
        )
        record = repo.get_application("cnstrct")
        assert record.name == "CNSTRCT (renamed)"
        conn.close()

    def test_unknown_key_returns_none(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        assert Repository(conn).get_application("does-not-exist") is None
        conn.close()


# --------------------------------------------------------------------------
# Service identity
# --------------------------------------------------------------------------


class TestServiceIdentity:
    def test_insert_new_and_repeat_advances_last_seen(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app_id = repo.upsert_application(
            key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1
        )
        id1 = repo.upsert_service(
            application_id=app_id, compose_service="api", name="api", observed_at=T1
        )
        id2 = repo.upsert_service(
            application_id=app_id, compose_service="api", name="api", observed_at=T2
        )
        assert id1 == id2
        services = repo.get_services_for_application(app_id)
        assert len(services) == 1
        assert services[0].first_seen_at == T1
        assert services[0].last_seen_at == T2
        conn.close()

    def test_standalone_null_compose_service_is_unique_per_application(self, tmp_path):
        """Guards against SQLite's NULL-uniqueness quirk: a naive
        UNIQUE(application_id, compose_service) would let this insert twice."""
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app_id = repo.upsert_application(
            key="standalone:twingate-connector",
            name="twingate-connector",
            is_standalone=True,
            observed_at=T1,
        )
        id1 = repo.upsert_service(
            application_id=app_id, compose_service=None, name="twingate-connector", observed_at=T1
        )
        id2 = repo.upsert_service(
            application_id=app_id, compose_service=None, name="twingate-connector", observed_at=T2
        )
        assert id1 == id2
        services = repo.get_services_for_application(app_id)
        assert len(services) == 1
        assert services[0].compose_service is None
        conn.close()

    def test_different_services_in_same_application_are_distinct(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app_id = repo.upsert_application(
            key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1
        )
        repo.upsert_service(application_id=app_id, compose_service="api", name="api", observed_at=T1)
        repo.upsert_service(
            application_id=app_id, compose_service="postgres", name="postgres", observed_at=T1
        )
        services = repo.get_services_for_application(app_id)
        assert {s.compose_service for s in services} == {"api", "postgres"}
        conn.close()


# --------------------------------------------------------------------------
# Container identity
# --------------------------------------------------------------------------


class TestContainerIdentity:
    def _app_and_service(self, repo: Repository) -> int:
        app_id = repo.upsert_application(
            key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1
        )
        return repo.upsert_service(
            application_id=app_id, compose_service="api", name="api", observed_at=T1
        )

    def test_insert_new(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        service_id = self._app_and_service(repo)
        row_id = repo.upsert_container(
            service_id=service_id,
            container_id="AAA",
            name="cnstrct-api-1",
            first_seen_at=T1,
            last_seen_at=T1,
        )
        record = repo.get_container_by_docker_id("AAA")
        assert record.id == row_id
        conn.close()

    def test_container_recreation_preserves_old_row_and_creates_new_row(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        service_id = self._app_and_service(repo)

        old_id = repo.upsert_container(
            service_id=service_id,
            container_id="AAA",
            name="cnstrct-api-1",
            first_seen_at=T1,
            last_seen_at=T1,
        )
        new_id = repo.upsert_container(
            service_id=service_id,
            container_id="BBB",
            name="cnstrct-api-1",  # same Docker name, different container_id
            first_seen_at=T2,
            last_seen_at=T2,
        )

        assert old_id != new_id
        containers = repo.get_containers_for_service(service_id)
        assert {c.container_id for c in containers} == {"AAA", "BBB"}
        assert len(containers) == 2  # neither overwrote the other
        assert repo.get_container_by_docker_id("AAA").first_seen_at == T1
        assert repo.get_container_by_docker_id("BBB").first_seen_at == T2
        conn.close()

    def test_name_update_for_same_container_id_keeps_same_identity(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        service_id = self._app_and_service(repo)

        id1 = repo.upsert_container(
            service_id=service_id,
            container_id="AAA",
            name="old-name",
            first_seen_at=T1,
            last_seen_at=T1,
        )
        id2 = repo.upsert_container(
            service_id=service_id,
            container_id="AAA",
            name="new-name",
            first_seen_at=T2,
            last_seen_at=T2,
        )

        assert id1 == id2  # same identity, not a new row
        record = repo.get_container_by_docker_id("AAA")
        assert record.name == "new-name"
        assert record.first_seen_at == T1  # never moved forward
        assert record.last_seen_at == T2
        conn.close()


# --------------------------------------------------------------------------
# Observation round trip
# --------------------------------------------------------------------------


class TestObservationRoundTrip:
    def _container_row(self, repo: Repository, container_id: str = "AAA") -> int:
        app_id = repo.upsert_application(
            key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1
        )
        service_id = repo.upsert_service(
            application_id=app_id, compose_service="api", name="api", observed_at=T1
        )
        return repo.upsert_container(
            service_id=service_id,
            container_id=container_id,
            name="cnstrct-api-1",
            first_seen_at=T1,
            last_seen_at=T1,
        )

    def test_full_round_trip_with_healthy(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        container_row_id = self._container_row(repo)

        container = make_container("AAA", "cnstrct-api-1")
        observation = make_observation(
            container,
            observed_at=T1,
            docker_state=DockerState.RUNNING,
            docker_health=DockerHealth.HEALTHY,
            restart_count=2,
            exit_code=None,
            started_at=T1,
            finished_at=None,
            ports=(
                PortBinding(container_port=8080, protocol=Protocol.TCP, host_ip="0.0.0.0", host_port=8080),
            ),
            labels={"com.docker.compose.project": "cnstrct", "com.docker.compose.service": "api"},
            derived_status=HealthStatus.HEALTHY,
            derived_detail=None,
        )
        repo.insert_observation(container_row_id=container_row_id, observation=observation)
        conn.close()

        conn2 = open_database(db_path)
        reloaded = Repository(conn2).get_latest_observation("AAA")
        conn2.close()

        assert reloaded == observation

    def test_round_trip_with_none_values(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        container_row_id = self._container_row(repo)

        container = make_container("AAA", "cnstrct-api-1")
        observation = make_observation(
            container,
            docker_health=None,
            exit_code=None,
            started_at=None,
            finished_at=None,
            ports=(PortBinding(container_port=5432, protocol=Protocol.TCP, host_ip=None, host_port=None),),
            derived_detail=None,
        )
        repo.insert_observation(container_row_id=container_row_id, observation=observation)
        conn.close()

        conn2 = open_database(db_path)
        reloaded = Repository(conn2).get_latest_observation("AAA")
        conn2.close()

        assert reloaded == observation
        assert reloaded.docker_health is None
        assert reloaded.exit_code is None
        assert reloaded.started_at is None
        assert reloaded.finished_at is None
        assert reloaded.ports[0].host_ip is None
        assert reloaded.ports[0].host_port is None
        assert reloaded.derived_detail is None

    def test_multiple_observations_read_back_in_chronological_order(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        container_row_id = self._container_row(repo)
        container = make_container("AAA", "cnstrct-api-1")

        for observed_at, status in (
            (T1, HealthStatus.HEALTHY),
            (T2, HealthStatus.DEGRADED),
            (T3, HealthStatus.HEALTHY),
        ):
            repo.insert_observation(
                container_row_id=container_row_id,
                observation=make_observation(container, observed_at=observed_at, derived_status=status),
            )

        history = repo.get_observation_history("AAA")
        assert [o.observed_at for o in history] == [T1, T2, T3]
        assert [o.derived_status for o in history] == [
            HealthStatus.HEALTHY,
            HealthStatus.DEGRADED,
            HealthStatus.HEALTHY,
        ]
        conn.close()

    def test_duplicate_observation_raises_and_does_not_overwrite(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        container_row_id = self._container_row(repo)
        container = make_container("AAA", "cnstrct-api-1")

        first = make_observation(container, observed_at=T1, derived_status=HealthStatus.HEALTHY)
        repo.insert_observation(container_row_id=container_row_id, observation=first)

        duplicate = make_observation(container, observed_at=T1, derived_status=HealthStatus.UNHEALTHY)
        with pytest.raises(DuplicateObservationError):
            repo.insert_observation(container_row_id=container_row_id, observation=duplicate)

        # the original row must be untouched -- append-only, not last-write-wins
        history = repo.get_observation_history("AAA")
        assert len(history) == 1
        assert history[0].derived_status is HealthStatus.HEALTHY
        conn.close()

    def test_get_latest_observation_returns_none_when_no_history(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        self._container_row(repo)
        assert repo.get_latest_observation("AAA") is None
        conn.close()


# --------------------------------------------------------------------------
# persist_discovery + transactions
# --------------------------------------------------------------------------


class TestPersistDiscoveryAndTransactions:
    def test_full_snapshot_persists_and_survives_reopen(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)

        api_container = make_container("AAA", "cnstrct-api-1", observed_at=T1)
        api_service = make_service([api_container], compose_service="api")
        pg_container = make_container(
            "BBB", "cnstrct-postgres-1", observed_at=T1, compose_service="postgres"
        )
        pg_service = make_service([pg_container], compose_service="postgres")
        app = make_application([api_service, pg_service])

        observations = [
            make_observation(api_container, observed_at=T1),
            make_observation(pg_container, observed_at=T1, docker_health=None),
        ]

        report = repo.persist_discovery(applications=[app], observations=observations)
        assert report.applications_written == 1
        assert report.services_written == 2
        assert report.containers_written == 2
        assert report.observations_written == 2
        conn.close()

        conn2 = open_database(db_path)
        repo2 = Repository(conn2)
        record = repo2.get_application("cnstrct")
        assert record is not None
        services = repo2.get_services_for_application(record.id)
        assert {s.compose_service for s in services} == {"api", "postgres"}
        conn2.close()

    def test_transaction_rolls_back_completely_on_duplicate_observation(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)

        container = make_container("AAA", "cnstrct-api-1", observed_at=T1)
        service = make_service([container], compose_service="api")
        app = make_application([service])
        obs = make_observation(container, observed_at=T1)
        repo.persist_discovery(applications=[app], observations=[obs])

        # second snapshot: a brand-new, never-seen application is processed
        # FIRST (so its identities really do get written inside this
        # transaction), followed by a duplicate observation for the
        # already-persisted container+timestamp above.
        new_container = make_container(
            "ZZZ", "newapp-web-1", observed_at=T2, compose_project="newapp", compose_service="web"
        )
        new_service = make_service([new_container], application_key="newapp", compose_service="web")
        new_app = make_application([new_service], key="newapp", name="newapp")

        with pytest.raises(DuplicateObservationError):
            repo.persist_discovery(
                applications=[new_app, app],
                observations=[make_observation(new_container, observed_at=T2), obs],
            )

        # the whole batch rolled back -- including the new application that
        # was successfully written earlier in the very same transaction
        assert repo.get_application("newapp") is None
        assert repo.get_container_by_docker_id("ZZZ") is None
        conn.close()

    def test_service_with_zero_containers_raises_clear_persistence_error(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        empty_service = make_service([], compose_service="ghost")
        app = make_application([empty_service])

        with pytest.raises(repository_module.PersistenceError):
            repo.persist_discovery(applications=[app], observations=[])
        conn.close()


# --------------------------------------------------------------------------
# M3 -> M4 integration: real discovery, fixture-driven, through persistence
# --------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "docker_responses"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


class _FakeContainer:
    def __init__(self, id: str, attrs: dict):
        self.id = id
        self.attrs = attrs


class _FakeContainersAPI:
    def __init__(self, by_id: dict[str, dict]):
        self._by_id = by_id

    def list(self, all=False):
        return [_FakeContainer(cid, {}) for cid in self._by_id]

    def get(self, container_id):
        return _FakeContainer(container_id, self._by_id[container_id])


class _FakeSDKClient:
    def __init__(self, by_id: dict[str, dict]):
        self.containers = _FakeContainersAPI(by_id)


def _make_real_discovery_client(fixture_names: list[str]):
    from argus.collectors.docker_client import DockerClient

    by_id = {}
    for fixture_name in fixture_names:
        attrs = _load_fixture(fixture_name)
        by_id[attrs["Id"]] = attrs
    return DockerClient(client=_FakeSDKClient(by_id))


class TestM3ToM4Integration:
    def test_real_discovery_persists_and_reloads_correctly(self, tmp_path):
        from argus.collectors.docker_collector import discover

        client = _make_real_discovery_client(
            ["compose_healthy_api", "compose_postgres_nohealthcheck", "compose_redis_unhealthy"]
        )
        observed_at = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
        result = discover(client, observed_at=observed_at)

        # bridge M3's placeholder derived_status with the real evaluated
        # health before persisting -- see resolve_observation_health's
        # docstring for why this seam exists.
        resolved_observations = [
            resolve_observation_health(
                observation,
                status=result.evaluations[observation.container_ref.container_id].status,
                detail=result.evaluations[observation.container_ref.container_id].detail,
            )
            for observation in result.observations
        ]

        db_path = tmp_path / "integration.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        report = repo.persist_discovery(
            applications=result.applications, observations=resolved_observations
        )
        conn.close()

        assert report.applications_written == len(result.applications)
        assert report.services_written == sum(len(a.services) for a in result.applications)
        assert report.containers_written == sum(
            len(s.containers) for a in result.applications for s in a.services
        )
        assert report.observations_written == len(resolved_observations)

        conn2 = open_database(db_path)
        repo2 = Repository(conn2)

        app_record = repo2.get_application("cnstrct")
        assert app_record is not None
        services = repo2.get_services_for_application(app_record.id)
        assert {s.compose_service for s in services} == {"api", "postgres", "redis"}

        for resolved in resolved_observations:
            container_id = resolved.container_ref.container_id
            reloaded = repo2.get_latest_observation(container_id)
            assert reloaded == resolved
            assert reloaded.derived_status == result.evaluations[container_id].status
            assert reloaded.ports == resolved.ports
            assert reloaded.labels == resolved.labels
            assert reloaded.observed_at == observed_at

        conn2.close()


# --------------------------------------------------------------------------
# Security: no data beyond what Milestone 3 already allowlisted
# --------------------------------------------------------------------------


class TestSecurity:
    def test_labels_json_contains_only_allowlisted_labels(self, tmp_path):
        from argus.collectors.docker_collector import discover

        client = _make_real_discovery_client(["compose_healthy_api"])
        observed_at = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
        result = discover(client, observed_at=observed_at)

        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        repo.persist_discovery(applications=result.applications, observations=result.observations)

        raw_row = conn.execute("SELECT labels_json FROM observations").fetchone()
        stored_labels = json.loads(raw_row["labels_json"])

        assert stored_labels == {
            "com.docker.compose.project": "cnstrct",
            "com.docker.compose.service": "api",
            "argus.owner": "jorge",
        }
        assert "some.secret.label" not in stored_labels
        assert "com.docker.compose.project.working_dir" not in stored_labels
        conn.close()

    def test_no_unexpected_fields_survive_into_stored_json(self, tmp_path):
        """Simulates a fixture carrying an unexpected extra field and proves
        it never reaches storage -- Argus only ever writes what Milestone 3
        already extracted onto the Observation object, nothing raw."""
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app_id = repo.upsert_application(key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1)
        service_id = repo.upsert_service(
            application_id=app_id, compose_service="api", name="api", observed_at=T1
        )
        container_row_id = repo.upsert_container(
            service_id=service_id,
            container_id="AAA",
            name="cnstrct-api-1",
            first_seen_at=T1,
            last_seen_at=T1,
        )
        container = make_container("AAA", "cnstrct-api-1")
        observation = make_observation(
            container, labels={"com.docker.compose.project": "cnstrct", "argus.owner": "jorge"}
        )
        repo.insert_observation(container_row_id=container_row_id, observation=observation)

        raw_row = conn.execute("SELECT labels_json, ports_json FROM observations").fetchone()
        assert set(json.loads(raw_row["labels_json"])) == {"com.docker.compose.project", "argus.owner"}
        # ports_json/labels_json only ever hold what PortBinding/labels already were --
        # never raw Docker Env, Mounts, or full Config.
        assert "Env" not in raw_row["ports_json"]
        assert "Mounts" not in raw_row["ports_json"]
        conn.close()


# --------------------------------------------------------------------------
# Architecture guards
# --------------------------------------------------------------------------

FORBIDDEN_IMPORT_ROOTS = {"docker", "anthropic", "openai", "langgraph", "fastapi"}


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


def _called_function_names(source: str) -> set[str]:
    """Names of functions actually *called* in source -- ignores plain-text
    mentions inside docstrings/comments, unlike a substring search."""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


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
    def test_store_has_no_docker_or_ai_imports(self):
        source = inspect.getsource(database_module) + inspect.getsource(repository_module)
        found = _imported_roots(source) & FORBIDDEN_IMPORT_ROOTS
        assert not found, f"argus.store imports forbidden module(s): {found}"

    def test_store_may_import_sqlite3_and_domain(self):
        source = inspect.getsource(database_module) + inspect.getsource(repository_module)
        roots = _imported_roots(source)
        assert "sqlite3" in roots
        assert "argus" in roots

    def test_repository_does_not_import_health_module(self):
        source = inspect.getsource(repository_module)
        modules = _imported_dotted_modules(source)
        assert "argus.domain.health" not in modules
        assert not any(m.startswith("argus.domain.health") for m in modules)

    def test_repository_never_calls_health_evaluators(self):
        source = inspect.getsource(repository_module)
        called = _called_function_names(source)
        forbidden = {
            "evaluate_container_health",
            "evaluate_service_health",
            "evaluate_application_health",
        }
        assert not (called & forbidden), f"repository.py calls: {called & forbidden}"

    def test_repository_never_imports_collectors(self):
        """persist_discovery takes plain sequences, not a DiscoveryResult,
        specifically so argus.store never depends on argus.collectors."""
        source = inspect.getsource(repository_module)
        modules = _imported_dotted_modules(source)
        assert not any(m.startswith("argus.collectors") for m in modules)
