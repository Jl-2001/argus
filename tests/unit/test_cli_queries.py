"""Tests for argus.cli.queries -- the staleness-aware read-model layer.

All against a temporary, file-backed database populated directly
through Repository -- no Docker, no collector loop.
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone

from argus.cli import queries as queries_module
from argus.cli.queries import (
    get_application_detail,
    get_collector_status,
    list_application_summaries,
    list_history,
    list_incidents,
    suggest_application_name,
)
from argus.domain.health import DEFAULT_HEALTH_RULES
from argus.domain.models import (
    Container,
    DockerHealth,
    DockerState,
    HealthStatus,
    Observation,
    PortBinding,
    Protocol,
)
from argus.store.database import open_database
from argus.store.repository import Repository

UTC = timezone.utc
NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
UNKNOWN_AFTER = DEFAULT_HEALTH_RULES.unknown_after


def seed_full_stack(repository: Repository, *, key, name, compose_service, container_id, at, status):
    """Seed identity + a full container/service/application transition set,
    the way the real incident engine always does together -- so tests
    here don't accidentally produce the "service never got a transition"
    gap that isn't representative of real operation."""

    repository.upsert_application(key=key, name=name, is_standalone=False, observed_at=at)
    app = repository.get_application(key)
    repository.upsert_service(application_id=app.id, compose_service=compose_service, name=compose_service, observed_at=at)
    service = repository.get_service_by_key(application_id=app.id, compose_service=compose_service)
    repository.upsert_container(
        service_id=service.id, container_id=container_id, name=f"{key}-{compose_service}-1",
        first_seen_at=at, last_seen_at=at,
    )
    container_record = repository.get_container_by_docker_id(container_id)

    repository.insert_transition(scope="container", scope_id=container_record.id, from_status=None, to_status=status, occurred_at=at)
    repository.insert_transition(scope="service", scope_id=service.id, from_status=None, to_status=status, occurred_at=at)
    repository.insert_transition(scope="application", scope_id=app.id, from_status=None, to_status=status, occurred_at=at)
    return app, service, container_record


class TestCollectorStatusClassification:
    def test_never_run(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        result = get_collector_status(repo, now=NOW)
        assert result.classification == "NEVER_RUN"
        assert result.data_is_fresh is False
        conn.close()

    def test_healthy(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        repo.record_tick_started(at=NOW - timedelta(seconds=5))
        repo.record_tick_success(at=NOW - timedelta(seconds=5))
        result = get_collector_status(repo, now=NOW)
        assert result.classification == "HEALTHY"
        assert result.data_is_fresh is True
        conn.close()

    def test_stale_when_last_tick_itself_is_old(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        old = NOW - timedelta(seconds=UNKNOWN_AFTER + 1)
        repo.record_tick_started(at=old)
        repo.record_tick_success(at=old)
        result = get_collector_status(repo, now=NOW)
        assert result.classification == "STALE"
        assert result.data_is_fresh is False
        conn.close()

    def test_failing_when_ticking_but_not_succeeding(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        repo.record_tick_started(at=NOW - timedelta(seconds=5))
        repo.record_tick_failure(error="DockerUnavailableError: boom")
        result = get_collector_status(repo, now=NOW)
        assert result.classification == "FAILING"
        assert result.consecutive_failures == 1
        assert result.last_error == "DockerUnavailableError: boom"
        conn.close()

    def test_failing_collector_has_stale_data_even_though_last_tick_is_fresh(self, tmp_path):
        """The key distinction: last_tick_at can be recent (the loop is
        still alive and retrying) while last_success_at is long past --
        data_is_fresh must track the latter, not the former."""

        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        old_success = NOW - timedelta(seconds=UNKNOWN_AFTER + 500)
        repo.record_tick_started(at=old_success)
        repo.record_tick_success(at=old_success)
        for _ in range(3):
            repo.record_tick_started(at=NOW - timedelta(seconds=5))
            repo.record_tick_failure(error="still down")

        result = get_collector_status(repo, now=NOW)
        assert result.classification == "FAILING"  # last_tick_at is fresh
        assert result.data_is_fresh is False  # but last_success_at is not
        conn.close()


class TestApplicationCurrentStatus:
    def test_fresh_data_reports_last_transition(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        seed_full_stack(repo, key="cnstrct", name="CNSTRCT", compose_service="api", container_id="aaa", at=NOW, status=HealthStatus.DEGRADED)
        repo.record_tick_started(at=NOW)
        repo.record_tick_success(at=NOW)

        summaries = list_application_summaries(repo, now=NOW)
        assert summaries[0].status is HealthStatus.DEGRADED
        conn.close()

    def test_stale_collector_overrides_to_unknown_regardless_of_last_transition(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        seed_full_stack(repo, key="cnstrct", name="CNSTRCT", compose_service="api", container_id="aaa", at=NOW, status=HealthStatus.HEALTHY)
        old = NOW - timedelta(seconds=UNKNOWN_AFTER + 500)
        repo.record_tick_started(at=old)
        repo.record_tick_success(at=old)

        summaries = list_application_summaries(repo, now=NOW)
        assert summaries[0].status is HealthStatus.UNKNOWN  # not HEALTHY, despite the stored transition
        conn.close()

    def test_no_transition_ever_recorded_is_unknown(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        repo.upsert_application(key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=NOW)
        repo.record_tick_started(at=NOW)
        repo.record_tick_success(at=NOW)

        summaries = list_application_summaries(repo, now=NOW)
        assert summaries[0].status is HealthStatus.UNKNOWN
        conn.close()


class TestApplicationLookup:
    def test_case_insensitive_by_key_and_name(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        seed_full_stack(repo, key="cnstrct", name="CNSTRCT", compose_service="api", container_id="aaa", at=NOW, status=HealthStatus.HEALTHY)
        repo.record_tick_started(at=NOW)
        repo.record_tick_success(at=NOW)

        assert get_application_detail(repo, now=NOW, name_or_key="cnstrct") is not None
        assert get_application_detail(repo, now=NOW, name_or_key="CNSTRCT") is not None
        assert get_application_detail(repo, now=NOW, name_or_key="cNsTrCt") is not None
        conn.close()

    def test_unknown_application_returns_none(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        assert get_application_detail(repo, now=NOW, name_or_key="ghost") is None
        conn.close()

    def test_suggestion_prefers_display_name_over_key(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        seed_full_stack(repo, key="cnstrct", name="CNSTRCT", compose_service="api", container_id="aaa", at=NOW, status=HealthStatus.HEALTHY)
        suggestion = suggest_application_name(repo, "cnstrt")
        assert suggestion == "CNSTRCT"
        conn.close()

    def test_no_suggestion_when_nothing_close(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        seed_full_stack(repo, key="cnstrct", name="CNSTRCT", compose_service="api", container_id="aaa", at=NOW, status=HealthStatus.HEALTHY)
        assert suggest_application_name(repo, "zzzzzzzzzz") is None
        conn.close()


class TestApplicationDetail:
    def test_ports_docker_state_health_restart_count_from_latest_observation(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app, service, container_record = seed_full_stack(
            repo, key="cnstrct", name="CNSTRCT", compose_service="api", container_id="aaa", at=NOW,
            status=HealthStatus.HEALTHY,
        )
        container = Container(
            container_id="aaa", name="cnstrct-api-1", image="cnstrct/api:1", compose_project="cnstrct",
            compose_service="api", first_seen_at=NOW, last_seen_at=NOW,
        )
        observation = Observation(
            container_ref=container, observed_at=NOW, docker_state=DockerState.RUNNING,
            docker_health=DockerHealth.HEALTHY, restart_count=2, exit_code=None, started_at=None,
            finished_at=None,
            ports=(PortBinding(container_port=3000, protocol=Protocol.TCP, host_ip="0.0.0.0", host_port=3000),),
            labels={"com.docker.compose.project": "cnstrct"}, derived_status=HealthStatus.HEALTHY,
            derived_detail=None,
        )
        repo.insert_observation(container_row_id=container_record.id, observation=observation)
        repo.record_tick_started(at=NOW)
        repo.record_tick_success(at=NOW)

        detail = get_application_detail(repo, now=NOW, name_or_key="cnstrct")
        service_detail = detail.services[0]
        assert service_detail.container.docker_state == "running"
        assert service_detail.container.docker_health == "healthy"
        assert service_detail.container.restart_count == 2
        assert service_detail.container.ports == (
            queries_module.PortView(container_port=3000, protocol="tcp", host_binding="0.0.0.0:3000"),
        )
        conn.close()

    def test_open_incident_populated(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app, *_ = seed_full_stack(repo, key="cnstrct", name="CNSTRCT", compose_service="api", container_id="aaa", at=NOW, status=HealthStatus.UNHEALTHY)
        transition = repo.get_last_transition(scope="application", scope_id=app.id)
        repo.open_incident(scope_id=app.id, failure_signature="application:cnstrct", opened_at=NOW, opening_status=HealthStatus.UNHEALTHY, opening_transition_id=transition.id)
        repo.record_tick_started(at=NOW)
        repo.record_tick_success(at=NOW)

        detail = get_application_detail(repo, now=NOW, name_or_key="cnstrct")
        assert detail.open_incident is not None
        assert detail.open_incident.status == "open"
        conn.close()

    def test_no_open_incident_is_none(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        seed_full_stack(repo, key="cnstrct", name="CNSTRCT", compose_service="api", container_id="aaa", at=NOW, status=HealthStatus.HEALTHY)
        repo.record_tick_started(at=NOW)
        repo.record_tick_success(at=NOW)
        detail = get_application_detail(repo, now=NOW, name_or_key="cnstrct")
        assert detail.open_incident is None
        conn.close()


class TestIncidentsAndHistory:
    def test_list_incidents_open_only_and_ordering(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app1, *_ = seed_full_stack(repo, key="a", name="A", compose_service="svc", container_id="c1", at=NOW - timedelta(hours=2), status=HealthStatus.UNHEALTHY)
        t1 = repo.get_last_transition(scope="application", scope_id=app1.id)
        repo.open_incident(scope_id=app1.id, failure_signature="application:a", opened_at=NOW - timedelta(hours=2), opening_status=HealthStatus.UNHEALTHY, opening_transition_id=t1.id)

        app2, *_ = seed_full_stack(repo, key="b", name="B", compose_service="svc", container_id="c2", at=NOW - timedelta(hours=1), status=HealthStatus.DEGRADED)
        t2 = repo.get_last_transition(scope="application", scope_id=app2.id)
        repo.open_incident(scope_id=app2.id, failure_signature="application:b", opened_at=NOW - timedelta(hours=1), opening_status=HealthStatus.DEGRADED, opening_transition_id=t2.id)

        all_incidents = list_incidents(repo)
        assert [i.application_key for i in all_incidents] == ["b", "a"]  # newest opened first

        open_only = list_incidents(repo, open_only=True)
        assert len(open_only) == 2
        conn.close()

    def test_list_history_returns_none_for_unknown_application(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        assert list_history(repo, name_or_key="ghost", since=NOW - timedelta(hours=1)) is None
        conn.close()

    def test_list_history_chronological_with_labels(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        seed_full_stack(repo, key="cnstrct", name="CNSTRCT", compose_service="api", container_id="aaa", at=NOW - timedelta(minutes=10), status=HealthStatus.HEALTHY)

        entries = list_history(repo, name_or_key="cnstrct", since=NOW - timedelta(hours=1))
        assert len(entries) == 3  # container, service, application
        assert {e.label for e in entries} == {"api", "application"}
        conn.close()


# --------------------------------------------------------------------------
# Architecture guard
# --------------------------------------------------------------------------

FORBIDDEN_IMPORT_ROOTS = {"docker", "anthropic", "openai", "langgraph", "fastapi", "requests", "httpx"}


def _imported_roots(source: str) -> set[str]:
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
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


def _called_function_names(source: str) -> set[str]:
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


class TestArchitectureGuard:
    def test_queries_has_no_forbidden_imports(self):
        source = inspect.getsource(queries_module)
        found = _imported_roots(source) & FORBIDDEN_IMPORT_ROOTS
        assert not found, f"argus.cli.queries imports forbidden module(s): {found}"

    def test_queries_does_not_import_collectors_or_incidents_engine(self):
        source = inspect.getsource(queries_module)
        modules = _imported_dotted_modules(source)
        assert not any(m.startswith("argus.collectors") for m in modules)
        assert not any(m.startswith("argus.incidents") for m in modules)

    def test_queries_never_calls_health_evaluators(self):
        source = inspect.getsource(queries_module)
        called = _called_function_names(source)
        forbidden = {"evaluate_container_health", "evaluate_service_health", "evaluate_application_health"}
        assert not (called & forbidden)

    def test_queries_never_calls_write_operations(self):
        source = inspect.getsource(queries_module)
        called = _called_function_names(source)
        forbidden = {
            "insert_transition", "insert_observation", "upsert_application", "upsert_service",
            "upsert_container", "persist_discovery", "record_tick_started", "record_tick_success",
            "record_tick_failure", "open_incident", "resolve_incident", "update_incident_worst_status",
        }
        assert not (called & forbidden), f"argus.cli.queries calls write operation(s): {called & forbidden}"
