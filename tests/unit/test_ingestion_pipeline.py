"""Milestone 16 -- `argus.ingestion.pipeline`: the one shared
persist-then-process-incidents pipeline both the local collector and
the agent ingestion route go through."""

from __future__ import annotations

from datetime import datetime, timezone

from argus.domain.host import LOCAL_HOST_KEY
from argus.domain.models import Application, Container, DockerState, HealthStatus, Observation, Service
from argus.ingestion.pipeline import (
    persist_snapshot,
    persist_snapshot_and_process_incidents,
    process_incidents_for_snapshot,
    rescope_applications_for_host,
)
from argus.store.database import open_database
from argus.store.repository import Repository

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _application(key: str = "cnstrct") -> Application:
    container = Container(
        container_id="c" * 64, name="api-1", image="app:latest", compose_project=key,
        compose_service="api", first_seen_at=T0, last_seen_at=T0,
    )
    service = Service(application_key=key, compose_service="api", containers=(container,), derived_status=HealthStatus.HEALTHY)
    return Application(key=key, name=key.upper(), is_standalone=False, services=(service,), derived_status=HealthStatus.HEALTHY)


def _observation(application: Application) -> Observation:
    container = application.services[0].containers[0]
    return Observation(
        container_ref=container, observed_at=T0, docker_state=DockerState.RUNNING, docker_health=None,
        restart_count=0, exit_code=None, started_at=T0, finished_at=None, ports=(), labels={},
        derived_status=HealthStatus.HEALTHY, derived_detail=None,
    )


class TestRescopeApplicationsForHost:
    def test_local_host_is_a_complete_no_op(self):
        application = _application()
        rescoped = rescope_applications_for_host((application,), host_key=LOCAL_HOST_KEY)
        assert rescoped[0] is application  # literally the same object, not just equal

    def test_non_local_host_rewrites_key_and_service_application_key(self):
        application = _application()
        rescoped = rescope_applications_for_host((application,), host_key="dell")
        assert rescoped[0].key == "dell:cnstrct"
        assert rescoped[0].services[0].application_key == "dell:cnstrct"

    def test_original_application_object_is_untouched(self):
        application = _application()
        rescope_applications_for_host((application,), host_key="dell")
        assert application.key == "cnstrct"  # frozen dataclass, never mutated in place


class TestPersistSnapshotAndProcessIncidents:
    def test_local_host_persists_under_the_unscoped_key(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        host_id = repo.ensure_local_host(display_name="Local Host", now=T0)
        application = _application()
        observation = _observation(application)

        persist_snapshot_and_process_incidents(
            repo, host_id=host_id, host_key=LOCAL_HOST_KEY,
            applications=(application,), observations=(observation,), tick_at=T0,
        )

        record = repo.get_application("cnstrct")
        assert record is not None
        assert record.host_id == host_id
        conn.close()

    def test_remote_host_persists_under_the_scoped_key(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        host_id = repo.create_agent_host(
            host_key="dell", agent_id="agent-1", display_name="Dell", token_hash="x" * 64, now=T0
        )
        application = _application()
        observation = _observation(application)

        persist_snapshot_and_process_incidents(
            repo, host_id=host_id, host_key="dell",
            applications=(application,), observations=(observation,), tick_at=T0,
        )

        assert repo.get_application("cnstrct") is None
        record = repo.get_application("dell:cnstrct")
        assert record is not None
        assert record.host_id == host_id
        conn.close()

    def test_incidents_are_created_through_the_same_engine(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        host_id = repo.ensure_local_host(display_name="Local Host", now=T0)

        healthy_app = _application()
        healthy_obs = _observation(healthy_app)
        persist_snapshot_and_process_incidents(
            repo, host_id=host_id, host_key=LOCAL_HOST_KEY,
            applications=(healthy_app,), observations=(healthy_obs,), tick_at=T0,
        )

        unhealthy_service = healthy_app.services[0].__class__(
            application_key="cnstrct", compose_service="api", containers=healthy_app.services[0].containers,
            derived_status=HealthStatus.UNHEALTHY,
        )
        unhealthy_app = Application(
            key="cnstrct", name="CNSTRCT", is_standalone=False, services=(unhealthy_service,),
            derived_status=HealthStatus.UNHEALTHY,
        )
        from datetime import timedelta

        t1 = T0 + timedelta(seconds=15)
        second_obs = Observation(
            container_ref=healthy_obs.container_ref, observed_at=t1, docker_state=DockerState.RUNNING,
            docker_health=None, restart_count=0, exit_code=None, started_at=T0, finished_at=None, ports=(),
            labels={}, derived_status=HealthStatus.UNHEALTHY, derived_detail=None,
        )
        _, incident_result = persist_snapshot_and_process_incidents(
            repo, host_id=host_id, host_key=LOCAL_HOST_KEY,
            applications=(unhealthy_app,), observations=(second_obs,), tick_at=t1,
        )
        assert incident_result.incidents_opened == 1
        conn.close()

    def test_split_persist_then_process_matches_the_combined_call(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        host_id = repo.ensure_local_host(display_name="Local Host", now=T0)
        application = _application()
        observation = _observation(application)

        _, scoped = persist_snapshot(
            repo, host_id=host_id, host_key=LOCAL_HOST_KEY, applications=(application,), observations=(observation,)
        )
        result = process_incidents_for_snapshot(repo, applications=scoped, observations=(observation,), tick_at=T0)
        assert result.transitions_created >= 1
        conn.close()
