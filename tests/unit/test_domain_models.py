"""Tests for argus.domain.models.

These tests must pass with Docker stopped, no SQLite database present,
and no network access — the domain package has no infrastructure
dependencies to exercise in the first place.
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from argus.domain import models
from argus.domain.models import (
    Application,
    Container,
    DockerHealth,
    DockerState,
    EVIDENCE_SEVERITY_RANK,
    EvidenceCategory,
    EvidenceRecord,
    EvidenceSeverity,
    HealthStatus,
    Observation,
    PortBinding,
    Protocol,
    Service,
    evidence_severity_rank,
)

UTC = timezone.utc


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


def make_container(
    *,
    container_id: str = "c" * 64,
    name: str = "api",
    image: str = "cnstrct/api:latest",
    compose_project: str | None = "cnstrct",
    compose_service: str | None = "api",
    first_seen_at: datetime = datetime(2026, 8, 21, 10, 0, 0, tzinfo=UTC),
    last_seen_at: datetime = datetime(2026, 8, 21, 10, 5, 0, tzinfo=UTC),
) -> Container:
    return Container(
        container_id=container_id,
        name=name,
        image=image,
        compose_project=compose_project,
        compose_service=compose_service,
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
    )


def make_observation(container: Container, **overrides) -> Observation:
    defaults = dict(
        container_ref=container,
        observed_at=datetime(2026, 8, 21, 10, 5, 0, tzinfo=UTC),
        docker_state=DockerState.RUNNING,
        docker_health=DockerHealth.HEALTHY,
        restart_count=0,
        exit_code=None,
        started_at=datetime(2026, 8, 21, 10, 0, 0, tzinfo=UTC),
        finished_at=None,
        ports=(PortBinding(container_port=8080, protocol=Protocol.TCP, host_port=8080),),
        labels={"com.docker.compose.project": "cnstrct", "com.docker.compose.service": "api"},
        derived_status=HealthStatus.HEALTHY,
        derived_detail=None,
    )
    defaults.update(overrides)
    return Observation(**defaults)


def make_cnstrct_application() -> Application:
    """CNSTRCT / api, postgres, redis, calc-engine — the brief's worked example."""

    def svc(name: str) -> Service:
        container = make_container(
            container_id=f"{name}-id-{'0' * 50}",
            name=f"cnstrct-{name}-1",
            image=f"cnstrct/{name}:latest",
            compose_project="cnstrct",
            compose_service=name,
        )
        return Service(
            application_key="cnstrct",
            compose_service=name,
            containers=(container,),
            derived_status=HealthStatus.HEALTHY,
        )

    return Application(
        key="cnstrct",
        name="CNSTRCT",
        is_standalone=False,
        services=tuple(svc(n) for n in ("api", "postgres", "redis", "calc-engine")),
        derived_status=HealthStatus.HEALTHY,
    )


# --------------------------------------------------------------------------
# Enum tests
# --------------------------------------------------------------------------


class TestEnums:
    def test_docker_state_values_exact(self):
        assert {e.value for e in DockerState} == {
            "running",
            "exited",
            "restarting",
            "paused",
            "dead",
            "created",
        }

    def test_docker_health_values_exact(self):
        assert {e.value for e in DockerHealth} == {"starting", "healthy", "unhealthy"}

    def test_docker_health_has_no_none_member(self):
        assert "none" not in {e.value for e in DockerHealth}
        assert not hasattr(DockerHealth, "NONE")

    def test_health_status_values_exact(self):
        assert {e.value for e in HealthStatus} == {
            "HEALTHY",
            "DEGRADED",
            "UNHEALTHY",
            "STOPPED",
            "RESTARTING",
            "UNKNOWN",
        }

    def test_health_status_has_no_stopped_subvariants(self):
        values = {e.value for e in HealthStatus}
        assert "STOPPED_ERROR" not in values
        assert "STOPPED_CLEAN" not in values

    def test_protocol_values_exact(self):
        assert {e.value for e in Protocol} == {"tcp", "udp"}


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------


class TestConstruction:
    def test_port_binding(self):
        p = PortBinding(container_port=8080, protocol=Protocol.TCP, host_ip="0.0.0.0", host_port=8080)
        assert p.container_port == 8080
        assert p.protocol is Protocol.TCP

    def test_container(self):
        c = make_container()
        assert c.container_id
        assert c.compose_project == "cnstrct"

    def test_observation(self):
        c = make_container()
        obs = make_observation(c)
        assert obs.container_ref is c
        assert obs.derived_status is HealthStatus.HEALTHY

    def test_service(self):
        c = make_container()
        s = Service(
            application_key="cnstrct",
            compose_service="api",
            containers=(c,),
            derived_status=HealthStatus.HEALTHY,
        )
        assert s.name == "api"
        assert s.containers == (c,)

    def test_application(self):
        app = make_cnstrct_application()
        assert app.name == "CNSTRCT"
        assert len(app.services) == 4
        assert {s.compose_service for s in app.services} == {
            "api",
            "postgres",
            "redis",
            "calc-engine",
        }

    def test_standalone_application_vs_compose_application(self):
        container = make_container(
            container_id="d" * 64,
            name="watchtower",
            compose_project=None,
            compose_service=None,
        )
        service = Service(
            application_key="standalone:watchtower",
            compose_service=None,
            containers=(container,),
            derived_status=HealthStatus.HEALTHY,
        )
        standalone_app = Application(
            key="standalone:watchtower",
            name="watchtower",
            is_standalone=True,
            services=(service,),
            derived_status=HealthStatus.HEALTHY,
        )
        compose_app = make_cnstrct_application()

        assert standalone_app.is_standalone is True
        assert compose_app.is_standalone is False
        assert len(standalone_app.services) == 1
        assert len(compose_app.services) == 4

    def test_service_application_key_must_match_application_key(self):
        container = make_container()
        mismatched_service = Service(
            application_key="not-cnstrct",
            compose_service="api",
            containers=(container,),
            derived_status=HealthStatus.HEALTHY,
        )
        with pytest.raises(ValueError, match="does not match"):
            Application(
                key="cnstrct",
                name="CNSTRCT",
                is_standalone=False,
                services=(mismatched_service,),
                derived_status=HealthStatus.HEALTHY,
            )

    def test_application_requires_at_least_one_service(self):
        with pytest.raises(ValueError, match="at least one Service"):
            Application(
                key="empty",
                name="Empty",
                is_standalone=False,
                services=(),
                derived_status=HealthStatus.UNKNOWN,
            )


# --------------------------------------------------------------------------
# UTC validation
# --------------------------------------------------------------------------


class TestUtcHandling:
    def test_utc_timestamp_accepted(self):
        make_container(
            first_seen_at=datetime(2026, 8, 21, 10, 0, 0, tzinfo=UTC),
            last_seen_at=datetime(2026, 8, 21, 10, 5, 0, tzinfo=UTC),
        )  # no raise

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            make_container(
                first_seen_at=datetime(2026, 8, 21, 10, 0, 0),  # naive
                last_seen_at=datetime(2026, 8, 21, 10, 5, 0, tzinfo=UTC),
            )

    def test_non_utc_offset_rejected(self):
        non_utc = timezone(timedelta(hours=5))
        with pytest.raises(ValueError, match="UTC"):
            make_container(
                first_seen_at=datetime(2026, 8, 21, 10, 0, 0, tzinfo=non_utc),
                last_seen_at=datetime(2026, 8, 21, 10, 5, 0, tzinfo=UTC),
            )

    def test_last_seen_before_first_seen_rejected(self):
        with pytest.raises(ValueError, match="last_seen_at cannot be before"):
            make_container(
                first_seen_at=datetime(2026, 8, 21, 10, 5, 0, tzinfo=UTC),
                last_seen_at=datetime(2026, 8, 21, 10, 0, 0, tzinfo=UTC),
            )

    def test_observation_naive_observed_at_rejected(self):
        c = make_container()
        with pytest.raises(ValueError, match="timezone-aware"):
            make_observation(c, observed_at=datetime(2026, 8, 21, 10, 5, 0))

    def test_observation_naive_started_at_rejected(self):
        c = make_container()
        with pytest.raises(ValueError, match="timezone-aware"):
            make_observation(c, started_at=datetime(2026, 8, 21, 10, 0, 0))


# --------------------------------------------------------------------------
# Optional values
# --------------------------------------------------------------------------


class TestOptionalValues:
    def test_docker_health_none(self):
        c = make_container()
        obs = make_observation(c, docker_health=None)
        assert obs.docker_health is None

    def test_exit_code_none(self):
        c = make_container()
        obs = make_observation(c, exit_code=None)
        assert obs.exit_code is None

    def test_host_ip_and_host_port_none(self):
        p = PortBinding(container_port=8080, protocol=Protocol.TCP, host_ip=None, host_port=None)
        assert p.host_ip is None
        assert p.host_port is None

    def test_compose_project_and_service_none(self):
        c = make_container(compose_project=None, compose_service=None)
        assert c.compose_project is None
        assert c.compose_service is None

    def test_finished_at_none_for_running_container(self):
        c = make_container()
        obs = make_observation(c, finished_at=None)
        assert obs.finished_at is None


# --------------------------------------------------------------------------
# Validation failures
# --------------------------------------------------------------------------


class TestValidationFailures:
    def test_negative_restart_count_rejected(self):
        c = make_container()
        with pytest.raises(ValueError, match="restart_count"):
            make_observation(c, restart_count=-1)

    def test_negative_exit_code_rejected(self):
        c = make_container()
        with pytest.raises(ValueError, match="exit_code"):
            make_observation(c, exit_code=-1)

    def test_invalid_protocol_rejected(self):
        with pytest.raises(ValueError):
            PortBinding(container_port=8080, protocol="http")

    def test_invalid_container_port_rejected(self):
        with pytest.raises(ValueError, match="container_port"):
            PortBinding(container_port=0, protocol=Protocol.TCP)
        with pytest.raises(ValueError, match="container_port"):
            PortBinding(container_port=70000, protocol=Protocol.TCP)

    def test_invalid_host_port_rejected(self):
        with pytest.raises(ValueError, match="host_port"):
            PortBinding(container_port=8080, protocol=Protocol.TCP, host_port=0)

    def test_empty_container_id_rejected(self):
        with pytest.raises(ValueError, match="container_id"):
            make_container(container_id="")

    def test_empty_application_key_rejected(self):
        with pytest.raises(ValueError, match="key"):
            Application(
                key="",
                name="Whatever",
                is_standalone=False,
                services=(
                    Service(
                        application_key="",
                        compose_service="api",
                        containers=(make_container(),),
                        derived_status=HealthStatus.HEALTHY,
                    ),
                ),
                derived_status=HealthStatus.HEALTHY,
            )

    def test_running_plus_unhealthy_is_a_valid_observation(self):
        """This module has no health opinions — that is Milestone 2's job."""
        c = make_container()
        obs = make_observation(
            c,
            docker_state=DockerState.RUNNING,
            docker_health=DockerHealth.UNHEALTHY,
            derived_status=HealthStatus.UNKNOWN,  # intentionally arbitrary
        )
        assert obs.docker_state is DockerState.RUNNING
        assert obs.docker_health is DockerHealth.UNHEALTHY


# --------------------------------------------------------------------------
# Serialization round-trip
# --------------------------------------------------------------------------


class TestSerializationRoundTrip:
    def test_application_round_trip(self):
        app = make_cnstrct_application()
        rebuilt = Application.from_dict(app.to_dict())
        assert rebuilt == app

    def test_observation_round_trip(self):
        c = make_container()
        obs = make_observation(c)
        rebuilt = Observation.from_dict(obs.to_dict())
        assert rebuilt == obs

    def test_round_trip_is_json_compatible(self):
        import json

        app = make_cnstrct_application()
        as_json = json.dumps(app.to_dict())
        rebuilt = Application.from_dict(json.loads(as_json))
        assert rebuilt == app

    def test_round_trip_preserves_optional_none_values(self):
        c = make_container(compose_project=None, compose_service=None)
        obs = make_observation(c, docker_health=None, exit_code=None, finished_at=None)
        rebuilt = Observation.from_dict(obs.to_dict())
        assert rebuilt.docker_health is None
        assert rebuilt.exit_code is None
        assert rebuilt.finished_at is None
        assert rebuilt.container_ref.compose_project is None


# --------------------------------------------------------------------------
# Evidence -- Milestone 10
# --------------------------------------------------------------------------


def make_evidence(**overrides) -> EvidenceRecord:
    fields = dict(
        id=1,
        application_key="cnstrct",
        container_id="docker-abc",
        category=EvidenceCategory.DB_CONNECTION_TIMEOUT,
        severity=EvidenceSeverity.HIGH,
        first_seen_at=datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC),
        last_seen_at=datetime(2026, 8, 22, 10, 0, 5, tzinfo=UTC),
        count=3,
        sample="connection timeout after 30s",
        source_type="container_log",
        source_ref="stdout+stderr",
    )
    fields.update(overrides)
    return EvidenceRecord(**fields)


class TestEvidenceSeverityRanking:
    def test_rank_is_explicit_not_declaration_order_or_string_sort(self):
        assert EVIDENCE_SEVERITY_RANK == {
            EvidenceSeverity.INFO: 1,
            EvidenceSeverity.WARNING: 2,
            EvidenceSeverity.HIGH: 3,
            EvidenceSeverity.CRITICAL: 4,
        }

    def test_evidence_severity_rank_function_matches_the_dict(self):
        for severity, rank in EVIDENCE_SEVERITY_RANK.items():
            assert evidence_severity_rank(severity) == rank

    def test_critical_outranks_everything_else(self):
        assert evidence_severity_rank(EvidenceSeverity.CRITICAL) > evidence_severity_rank(EvidenceSeverity.HIGH)
        assert evidence_severity_rank(EvidenceSeverity.HIGH) > evidence_severity_rank(EvidenceSeverity.WARNING)
        assert evidence_severity_rank(EvidenceSeverity.WARNING) > evidence_severity_rank(EvidenceSeverity.INFO)


class TestEvidenceCategoryEnum:
    def test_exactly_twelve_categories(self):
        assert len(EvidenceCategory) == 12

    def test_container_restart_and_unhealthy_are_present(self):
        assert EvidenceCategory.CONTAINER_RESTART.value == "container_restart"
        assert EvidenceCategory.CONTAINER_UNHEALTHY.value == "container_unhealthy"


class TestEvidenceRecordConstruction:
    def test_valid_record_constructs(self):
        evidence = make_evidence()
        assert evidence.category is EvidenceCategory.DB_CONNECTION_TIMEOUT
        assert evidence.severity is EvidenceSeverity.HIGH

    def test_string_category_and_severity_are_coerced_to_enum(self):
        evidence = make_evidence(category="oom", severity="critical")
        assert evidence.category is EvidenceCategory.OOM
        assert evidence.severity is EvidenceSeverity.CRITICAL

    def test_id_may_be_none_before_persistence(self):
        evidence = make_evidence(id=None)
        assert evidence.id is None

    def test_last_seen_before_first_seen_rejected(self):
        with pytest.raises(ValueError):
            make_evidence(
                first_seen_at=datetime(2026, 8, 22, 10, 0, 5, tzinfo=UTC),
                last_seen_at=datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC),
            )

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValueError):
            make_evidence(first_seen_at=datetime(2026, 8, 22, 10, 0, 0))

    def test_zero_count_rejected(self):
        with pytest.raises(ValueError):
            make_evidence(count=0)

    def test_negative_count_rejected(self):
        with pytest.raises(ValueError):
            make_evidence(count=-1)

    def test_empty_sample_rejected(self):
        with pytest.raises(ValueError):
            make_evidence(sample="")

    def test_invalid_source_type_rejected(self):
        with pytest.raises(ValueError):
            make_evidence(source_type="something_else")

    def test_empty_application_key_rejected(self):
        with pytest.raises(ValueError):
            make_evidence(application_key="")

    def test_empty_container_id_rejected(self):
        with pytest.raises(ValueError):
            make_evidence(container_id="")

    def test_unknown_category_string_rejected(self):
        with pytest.raises(ValueError):
            make_evidence(category="not_a_real_category")


class TestEvidenceRecordSerialization:
    def test_round_trip_preserves_every_field(self):
        original = make_evidence()
        rebuilt = EvidenceRecord.from_dict(original.to_dict())
        assert rebuilt == original

    def test_to_dict_uses_plain_json_safe_values(self):
        payload = make_evidence().to_dict()
        assert payload["category"] == "db_connection_timeout"
        assert payload["severity"] == "high"
        assert isinstance(payload["first_seen_at"], str)
        assert isinstance(payload["count"], int)

    def test_from_dict_with_no_id_key_defaults_to_none(self):
        payload = make_evidence().to_dict()
        del payload["id"]
        rebuilt = EvidenceRecord.from_dict(payload)
        assert rebuilt.id is None


# --------------------------------------------------------------------------
# Architecture guard
# --------------------------------------------------------------------------


FORBIDDEN_IMPORT_ROOTS = {
    "docker",
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
    def test_domain_module_has_no_infrastructure_imports(self):
        source = inspect.getsource(models)
        found = _imported_roots(source) & FORBIDDEN_IMPORT_ROOTS
        assert not found, f"argus.domain.models imports forbidden module(s): {found}"
