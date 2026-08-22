"""Tests for argus.collectors.docker_collector.

Fixtures live under tests/fixtures/docker_responses/ as sanitized,
realistic container.attrs-shaped JSON -- some synthetic, a few modeled
on real (sanitized) local Docker metadata. None of these tests require
a real Docker daemon.
"""

from __future__ import annotations

import ast
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import docker.errors
import pytest

from argus.collectors import docker_client, docker_collector
from argus.collectors.docker_client import ContainerAttrs, DockerClient
from argus.collectors.docker_collector import (
    DEFAULT_ALLOWED_LABEL_KEYS,
    DEFAULT_ALLOWED_LABEL_PREFIXES,
    TimestampParseError,
    UnknownDockerHealthError,
    UnknownDockerStateError,
    discover,
    parse_container,
)
from argus.domain import models as domain_models
from argus.domain.health import evaluate_container_health
from argus.domain.models import DockerHealth, DockerState, HealthStatus, Protocol

UTC = timezone.utc
OBSERVED_AT = datetime(2026, 8, 21, 10, 0, 0, tzinfo=UTC)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "docker_responses"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


def parse_fixture(name: str, **overrides):
    return parse_container(load_fixture(name), observed_at=OBSERVED_AT, **overrides)


# --------------------------------------------------------------------------
# Fake Docker SDK plumbing for discover()-level (end-to-end) tests
# --------------------------------------------------------------------------


class _FakeContainer:
    def __init__(self, id: str, attrs: dict):
        self.id = id
        self.attrs = attrs


class _FakeContainersAPI:
    def __init__(self, by_id: dict[str, dict], vanished: set[str] = frozenset()):
        self._by_id = by_id
        self._vanished = vanished

    def list(self, all=False):
        return [_FakeContainer(cid, {}) for cid in self._by_id]

    def get(self, container_id):
        if container_id in self._vanished:
            raise docker.errors.NotFound(container_id)
        return _FakeContainer(container_id, self._by_id[container_id])


class _FakeSDKClient:
    def __init__(self, by_id, vanished=frozenset()):
        self.containers = _FakeContainersAPI(by_id, vanished)


def make_client(fixture_names: list[str], vanished_fixture_names: list[str] = ()) -> DockerClient:
    """Build a real DockerClient wired to a fake SDK backed by fixtures.

    ``vanished_fixture_names`` marks fixtures that are listed (their id
    comes back from list_containers) but raise NotFound on inspect --
    simulating a container that disappeared between the two calls.
    """

    by_id = {}
    id_by_fixture = {}
    for fixture_name in fixture_names:
        attrs = load_fixture(fixture_name)
        by_id[attrs["Id"]] = attrs
        id_by_fixture[fixture_name] = attrs["Id"]
    vanished_ids = {id_by_fixture[name] for name in vanished_fixture_names}
    return DockerClient(client=_FakeSDKClient(by_id, vanished=vanished_ids))


# --------------------------------------------------------------------------
# parse_container -- identity, state, health, ports, labels
# --------------------------------------------------------------------------


class TestParseContainerIdentity:
    def test_extracts_identity_fields(self):
        container, observation = parse_fixture("compose_healthy_api")
        assert container.name == "cnstrct-api-1"
        assert container.image == "cnstrct/api:latest"
        assert container.compose_project == "cnstrct"
        assert container.compose_service == "api"

    def test_first_and_last_seen_at_are_observed_at_not_docker_started_at(self):
        container, _ = parse_fixture("compose_healthy_api")
        assert container.first_seen_at == OBSERVED_AT
        assert container.last_seen_at == OBSERVED_AT
        # Docker's own StartedAt in the fixture is a different, earlier time --
        # confirming we did not fake history from it.
        assert container.first_seen_at != datetime(2026, 8, 21, 9, 0, 0, tzinfo=UTC)

    def test_observation_carries_placeholder_derived_status(self):
        _, observation = parse_fixture("compose_healthy_api")
        assert observation.derived_status is HealthStatus.UNKNOWN
        assert observation.derived_detail is None


class TestParseContainerStates:
    @pytest.mark.parametrize(
        "fixture, expected_state",
        [
            ("compose_healthy_api", DockerState.RUNNING),
            ("stopped_clean", DockerState.EXITED),
            ("restarting_container", DockerState.RESTARTING),
            ("paused_container", DockerState.PAUSED),
            ("dead_container", DockerState.DEAD),
            ("zero_timestamps", DockerState.CREATED),
        ],
    )
    def test_docker_state_mapped_correctly(self, fixture, expected_state):
        _, observation = parse_fixture(fixture)
        assert observation.docker_state is expected_state

    def test_unknown_docker_state_raises_without_weakening_the_enum(self):
        with pytest.raises(UnknownDockerStateError):
            parse_fixture("malformed_unknown_state")
        # the enum itself must remain exactly the 6 known members
        assert {e.value for e in DockerState} == {
            "running",
            "exited",
            "restarting",
            "paused",
            "dead",
            "created",
        }


class TestParseContainerHealth:
    @pytest.mark.parametrize(
        "fixture, expected_health",
        [
            ("compose_healthy_api", DockerHealth.HEALTHY),
            ("compose_redis_unhealthy", DockerHealth.UNHEALTHY),
            ("compose_calc_engine_starting", DockerHealth.STARTING),
            ("compose_postgres_nohealthcheck", None),
        ],
    )
    def test_docker_health_mapped_correctly(self, fixture, expected_health):
        _, observation = parse_fixture(fixture)
        assert observation.docker_health == expected_health

    def test_unknown_health_status_raises(self):
        attrs = load_fixture("compose_healthy_api")
        attrs["State"]["Health"]["Status"] = "quantum-superposition"
        with pytest.raises(UnknownDockerHealthError):
            parse_container(attrs, observed_at=OBSERVED_AT)

    def test_health_object_present_but_empty_status_raises(self):
        attrs = load_fixture("compose_healthy_api")
        attrs["State"]["Health"]["Status"] = ""
        with pytest.raises(UnknownDockerHealthError):
            parse_container(attrs, observed_at=OBSERVED_AT)


class TestParseContainerPorts:
    def test_bound_tcp_port(self):
        _, observation = parse_fixture("compose_healthy_api")
        assert len(observation.ports) == 1
        port = observation.ports[0]
        assert port.container_port == 8080
        assert port.protocol is Protocol.TCP
        assert port.host_ip == "0.0.0.0"
        assert port.host_port == 8080

    def test_unbound_port_has_no_host_binding(self):
        _, observation = parse_fixture("unbound_port")
        assert len(observation.ports) == 1
        port = observation.ports[0]
        assert port.container_port == 5432
        assert port.protocol is Protocol.TCP
        assert port.host_ip is None
        assert port.host_port is None

    def test_multiple_host_bindings_all_preserved(self):
        _, observation = parse_fixture("multiple_port_bindings")
        tcp_bindings = [p for p in observation.ports if p.container_port == 80]
        assert len(tcp_bindings) == 2
        assert {p.host_ip for p in tcp_bindings} == {"0.0.0.0", "::"}

    def test_udp_protocol_supported(self):
        _, observation = parse_fixture("multiple_port_bindings")
        udp_bindings = [p for p in observation.ports if p.protocol is Protocol.UDP]
        assert len(udp_bindings) == 1
        assert udp_bindings[0].container_port == 53

    def test_malformed_port_entry_is_isolated_not_fatal(self):
        attrs = load_fixture("compose_healthy_api")
        attrs["NetworkSettings"]["Ports"]["not-a-real-port/tcp"] = [{"HostIp": "0.0.0.0", "HostPort": "x"}]
        # the malformed extra entry must not prevent parsing the rest of the container
        container, observation = parse_container(attrs, observed_at=OBSERVED_AT)
        assert container.name == "cnstrct-api-1"
        assert any(p.container_port == 8080 for p in observation.ports)


class TestParseContainerLabels:
    def test_only_allowlisted_labels_survive(self):
        _, observation = parse_fixture("compose_healthy_api")
        assert observation.labels == {
            "com.docker.compose.project": "cnstrct",
            "com.docker.compose.service": "api",
            "argus.owner": "jorge",
        }
        assert "some.secret.label" not in observation.labels
        assert "com.example.random" not in observation.labels
        assert "com.docker.compose.project.working_dir" not in observation.labels

    def test_sensitive_labels_stripped_on_standalone_container(self):
        _, observation = parse_fixture("sensitive_labels")
        assert observation.labels == {"argus.owner": "jorge"}
        assert "vendor.internal.build-path" not in observation.labels

    def test_compose_working_dir_label_never_leaks(self):
        """This is the label a broad `com.docker.compose.*` prefix allowlist
        would have leaked -- confirmed by inspecting real local containers
        while building this milestone. Guards against regressing to that."""
        _, observation = parse_fixture("compose_healthy_api")
        assert not any("working_dir" in key or "config_files" in key for key in observation.labels)


# --------------------------------------------------------------------------
# Timestamp parsing
# --------------------------------------------------------------------------


class TestTimestampParsing:
    def test_normal_timestamp_becomes_utc_aware(self):
        _, observation = parse_fixture("stopped_clean")
        assert observation.started_at == datetime(2026, 8, 21, 3, 0, 0, tzinfo=UTC)
        assert observation.started_at.tzinfo is not None

    def test_nine_digit_nanosecond_fraction_truncates_to_microseconds(self):
        # modeled on real local Docker metadata: "2026-08-09T16:32:49.913490667Z"
        _, observation = parse_fixture("real_world_shape_db")
        assert observation.started_at == datetime(2026, 8, 9, 16, 32, 49, 913490, tzinfo=UTC)

    def test_eight_digit_fraction_from_real_docker_output_parses_correctly(self):
        # modeled on real local Docker metadata: "2026-08-11T23:10:21.16776563Z"
        # (Docker/Go trims trailing zeros, so the digit count varies)
        _, observation = parse_fixture("real_world_shape_api")
        assert observation.finished_at == datetime(2026, 8, 11, 23, 10, 21, 167765, tzinfo=UTC)

    def test_zero_sentinel_becomes_none(self):
        _, observation = parse_fixture("zero_timestamps")
        assert observation.started_at is None
        assert observation.finished_at is None

    def test_malformed_timestamp_raises_rather_than_guessing(self):
        with pytest.raises(TimestampParseError):
            parse_fixture("malformed_timestamp")


# --------------------------------------------------------------------------
# Exit code / restart count -- faithfully translated, not interpreted
# --------------------------------------------------------------------------


class TestExitAndRestartMetadata:
    def test_exit_code_and_restart_count_passed_through_unmodified(self):
        _, observation = parse_fixture("stopped_error")
        assert observation.exit_code == 137
        assert observation.restart_count == 2
        # discovery must NOT decide stopped_clean/stopped_error -- that stays UNKNOWN here
        assert observation.derived_status is HealthStatus.UNKNOWN

    def test_clean_exit_code_passed_through(self):
        _, observation = parse_fixture("stopped_clean")
        assert observation.exit_code == 0


# --------------------------------------------------------------------------
# Health evaluator integration -- the required pipeline proof
# --------------------------------------------------------------------------


class TestHealthEvaluatorIntegration:
    def test_docker_fixture_to_observation_to_healthy(self):
        _, observation = parse_fixture("compose_healthy_api")
        result = evaluate_container_health(observation=observation, now=OBSERVED_AT)
        assert result.status is HealthStatus.HEALTHY

    def test_docker_fixture_to_observation_to_unhealthy(self):
        _, observation = parse_fixture("compose_redis_unhealthy")
        result = evaluate_container_health(observation=observation, now=OBSERVED_AT)
        assert result.status is HealthStatus.UNHEALTHY

    def test_docker_fixture_to_observation_to_degraded_starting(self):
        _, observation = parse_fixture("compose_calc_engine_starting")
        result = evaluate_container_health(observation=observation, now=OBSERVED_AT)
        assert result.status is HealthStatus.DEGRADED

    def test_docker_fixture_to_observation_to_stopped_error(self):
        _, observation = parse_fixture("stopped_error")
        result = evaluate_container_health(observation=observation, now=OBSERVED_AT)
        assert result == docker_collector.HealthEvaluation(HealthStatus.STOPPED, "stopped_error")


# --------------------------------------------------------------------------
# discover() -- application/service grouping
# --------------------------------------------------------------------------


class TestComposeGrouping:
    def test_three_services_one_project_become_one_application(self):
        client = make_client(
            ["compose_healthy_api", "compose_postgres_nohealthcheck", "compose_redis_unhealthy"]
        )
        result = discover(client, observed_at=OBSERVED_AT)

        assert len(result.applications) == 1
        app = result.applications[0]
        assert app.key == "cnstrct"
        assert app.is_standalone is False
        assert len(app.services) == 3
        assert {s.compose_service for s in app.services} == {"api", "postgres", "redis"}
        assert sum(len(s.containers) for s in app.services) == 3

    def test_separate_compose_projects_become_separate_applications(self):
        client = make_client(["compose_healthy_api", "musipal_api_healthy"])
        result = discover(client, observed_at=OBSERVED_AT)

        assert len(result.applications) == 2
        keys = {app.key for app in result.applications}
        assert keys == {"cnstrct", "musipal"}

    def test_application_rollup_reflects_worst_service(self):
        # api healthy, postgres healthy(no-hc), redis unhealthy -> app UNHEALTHY
        client = make_client(
            ["compose_healthy_api", "compose_postgres_nohealthcheck", "compose_redis_unhealthy"]
        )
        result = discover(client, observed_at=OBSERVED_AT)
        app = result.applications[0]
        assert app.derived_status is HealthStatus.UNHEALTHY


class TestStandaloneGrouping:
    def test_standalone_container_becomes_its_own_application(self):
        client = make_client(["standalone_twingate"])
        result = discover(client, observed_at=OBSERVED_AT)

        assert len(result.applications) == 1
        app = result.applications[0]
        assert app.key == "standalone:twingate-connector"
        assert app.is_standalone is True
        assert len(app.services) == 1
        assert app.services[0].compose_service is None

    def test_unrelated_standalone_containers_are_not_grouped_together(self):
        client = make_client(["standalone_twingate", "stopped_clean", "stopped_error"])
        result = discover(client, observed_at=OBSERVED_AT)

        assert len(result.applications) == 3
        assert len({app.key for app in result.applications}) == 3
        assert all(app.is_standalone for app in result.applications)


class TestDuplicateComposeService:
    def test_two_containers_same_project_and_service_are_not_silently_collapsed(self):
        client = make_client(["duplicate_service_a", "duplicate_service_b", "musipal_api_healthy"])
        result = discover(client, observed_at=OBSERVED_AT)

        # the conflicting application is excluded, not silently resolved by picking one
        app_keys = {app.key for app in result.applications}
        assert "dupproj" not in app_keys
        # the unrelated, valid application is still discovered
        assert "musipal" in app_keys

        assert len(result.skipped) == 1
        skip = result.skipped[0]
        assert skip.scope == "application"
        assert skip.identifier == "dupproj"
        assert "one container per service" in skip.reason


# --------------------------------------------------------------------------
# Per-container failure isolation
# --------------------------------------------------------------------------


class TestPerContainerIsolation:
    def test_one_malformed_container_does_not_prevent_others(self):
        client = make_client(
            ["compose_healthy_api", "musipal_api_healthy", "malformed_unknown_state"]
        )
        result = discover(client, observed_at=OBSERVED_AT)

        assert len(result.applications) == 2
        assert len(result.skipped) == 1
        assert result.skipped[0].scope == "container"

    def test_container_vanished_between_list_and_inspect_is_skipped_quietly(self):
        client = make_client(
            ["compose_healthy_api", "musipal_api_healthy"],
            vanished_fixture_names=["musipal_api_healthy"],
        )

        result = discover(client, observed_at=OBSERVED_AT)

        assert len(result.applications) == 1
        assert result.applications[0].key == "cnstrct"
        # a vanished container is not reported as a parsing failure -- it's routine
        assert result.skipped == ()


# --------------------------------------------------------------------------
# History provider (Milestone 9 regression)
#
# Before Milestone 9, argus.collector.loop.CollectorLoop.run_once() called
# discover() with no way at all to supply persisted history, so every real
# tick evaluated every container with prior_observations=() -- restart-loop
# / recent-restart detection was reachable only from discover()'s own
# direct unit tests, never from a real running collector. These tests
# cover discover()'s half of the fix: an optional history_provider that,
# when given, is actually threaded into evaluate_container_health.
# CollectorLoop's half (building a real, repository-backed provider) has
# its own regression in test_collector_loop.py.
# --------------------------------------------------------------------------


class TestHistoryProvider:
    def test_without_history_provider_prior_observations_stays_empty(self):
        attrs = load_fixture("compose_healthy_api")
        attrs["RestartCount"] = 3
        client = DockerClient(client=_FakeSDKClient({attrs["Id"]: attrs}))

        result = discover(client, observed_at=OBSERVED_AT)

        # No history_provider supplied -> prior_observations=() -- exactly
        # Milestone 3's original stateless behavior -- so no restart-loop
        # detection is possible here, no matter how high restart_count is.
        assert result.evaluations[attrs["Id"]].status is HealthStatus.HEALTHY

    def test_history_provider_feeds_real_restart_loop_detection(self):
        attrs = load_fixture("compose_healthy_api")
        attrs["RestartCount"] = 3
        container_id = attrs["Id"]
        client = DockerClient(client=_FakeSDKClient({container_id: attrs}))

        container, _ = parse_container(attrs, observed_at=OBSERVED_AT)
        prior = domain_models.Observation(
            container_ref=container,
            observed_at=OBSERVED_AT - timedelta(seconds=30),
            docker_state=DockerState.RUNNING,
            docker_health=DockerHealth.HEALTHY,
            restart_count=0,
            exit_code=None,
            started_at=None,
            finished_at=None,
            ports=(),
            labels={},
            derived_status=HealthStatus.HEALTHY,
            derived_detail=None,
        )

        def history_provider(cid: str):
            return [prior] if cid == container_id else []

        result = discover(client, observed_at=OBSERVED_AT, history_provider=history_provider)

        # baseline restart_count 0 (from the supplied prior observation),
        # current restart_count 3, both inside the default 300s window ->
        # delta 3 meets the default restart_loop_threshold of 3 ->
        # RESTARTING, not HEALTHY -- proving the provider's history
        # actually reached evaluate_container_health.
        assert result.evaluations[container_id].status is HealthStatus.RESTARTING
        assert result.evaluations[container_id].detail == "restart_loop"

    def test_history_provider_called_once_per_discovered_container(self):
        attrs = load_fixture("compose_healthy_api")
        client = DockerClient(client=_FakeSDKClient({attrs["Id"]: attrs}))
        calls: list[str] = []

        def history_provider(cid: str):
            calls.append(cid)
            return []

        discover(client, observed_at=OBSERVED_AT, history_provider=history_provider)

        assert calls == [attrs["Id"]]


# --------------------------------------------------------------------------
# Discovery result shape
# --------------------------------------------------------------------------


class TestDiscoveryResultShape:
    def test_no_raw_docker_payload_in_result(self):
        client = make_client(["compose_healthy_api"])
        result = discover(client, observed_at=OBSERVED_AT)
        # the result must be built from parsed domain objects only
        assert result.observations[0].labels == {
            "com.docker.compose.project": "cnstrct",
            "com.docker.compose.service": "api",
            "argus.owner": "jorge",
        }

    def test_evaluations_keyed_by_container_id_and_match_observations(self):
        client = make_client(["compose_healthy_api"])
        result = discover(client, observed_at=OBSERVED_AT)
        container_id = result.observations[0].container_ref.container_id
        assert result.evaluations[container_id].status is HealthStatus.HEALTHY


# --------------------------------------------------------------------------
# Architecture guard
# --------------------------------------------------------------------------

FORBIDDEN_IMPORT_ROOTS = {
    "sqlite3",
    "sqlalchemy",
    "anthropic",
    "openai",
    "langgraph",
    "fastapi",
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


class TestArchitectureGuard:
    def test_docker_collector_has_no_persistence_or_ai_imports(self):
        source = inspect.getsource(docker_collector)
        found = _imported_roots(source) & FORBIDDEN_IMPORT_ROOTS
        assert not found, f"docker_collector.py imports forbidden module(s): {found}"

    def test_domain_still_does_not_import_docker(self):
        """Re-confirms the Milestone 1/2 guard still holds now that a
        sibling package (argus.collectors) legitimately imports docker."""
        for module in (domain_models, __import__("argus.domain.health", fromlist=["_"])):
            source = inspect.getsource(module)
            roots = _imported_roots(source)
            assert "docker" not in roots, f"{module.__name__} must not import docker"

    def test_collectors_may_import_docker_and_domain(self):
        source = inspect.getsource(docker_collector) + inspect.getsource(docker_client)
        roots = _imported_roots(source)
        assert "docker" in roots
        assert "argus" in roots
