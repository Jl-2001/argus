"""Milestone 16 -- `argus.agent.protocol`: the wire contract. Plain
JSON round-trips, protocol version enforcement, and bounds."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from argus.agent.protocol import (
    MAX_SAMPLE_LENGTH,
    PROTOCOL_VERSION,
    AgentSnapshot,
    EvidenceCandidateWire,
    ProtocolError,
)
from argus.domain.models import (
    Application,
    Container,
    DockerState,
    EvidenceCategory,
    EvidenceSeverity,
    HealthStatus,
    Observation,
    Service,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _make_application() -> Application:
    container = Container(
        container_id="c" * 64, name="api-1", image="cnstrct/api:latest",
        compose_project="cnstrct", compose_service="api", first_seen_at=T0, last_seen_at=T0,
    )
    service = Service(
        application_key="cnstrct", compose_service="api", containers=(container,),
        derived_status=HealthStatus.HEALTHY,
    )
    return Application(
        key="cnstrct", name="CNSTRCT", is_standalone=False, services=(service,),
        derived_status=HealthStatus.HEALTHY,
    )


def _make_observation(application: Application) -> Observation:
    container = application.services[0].containers[0]
    return Observation(
        container_ref=container, observed_at=T0, docker_state=DockerState.RUNNING, docker_health=None,
        restart_count=0, exit_code=None, started_at=T0, finished_at=None, ports=(), labels={},
        derived_status=HealthStatus.HEALTHY, derived_detail=None,
    )


def _make_evidence_wire() -> EvidenceCandidateWire:
    return EvidenceCandidateWire(
        application_key="cnstrct", container_id="c" * 64, category=EvidenceCategory.CONTAINER_RESTART,
        severity=EvidenceSeverity.WARNING, normalized_signature="restart", first_seen_at=T0, last_seen_at=T0,
        count=1, sample="container restarted", source_type="docker_fact", source_ref="restart_count",
    )


class TestSnapshotRoundTrip:
    def test_to_dict_from_dict_round_trips(self):
        application = _make_application()
        observation = _make_observation(application)
        snapshot = AgentSnapshot(
            protocol_version=PROTOCOL_VERSION, agent_id="agent-1", host_key="dell", generated_at=T0,
            agent_version="0.1.0", applications=(application,), observations=(observation,),
            evidence_candidates=(_make_evidence_wire(),),
        )
        restored = AgentSnapshot.from_dict(snapshot.to_dict())
        assert restored.agent_id == "agent-1"
        assert restored.host_key == "dell"
        assert restored.applications[0].key == "cnstrct"
        assert restored.observations[0].container_ref.container_id == "c" * 64
        assert restored.evidence_candidates[0].category is EvidenceCategory.CONTAINER_RESTART

    def test_serialization_is_plain_json_compatible(self):
        import json

        application = _make_application()
        observation = _make_observation(application)
        snapshot = AgentSnapshot(
            protocol_version=PROTOCOL_VERSION, agent_id="agent-1", host_key="dell", generated_at=T0,
            agent_version="0.1.0", applications=(application,), observations=(observation,),
            evidence_candidates=(),
        )
        # Must not raise -- proves nothing here relies on pickle or any
        # non-JSON-serializable type.
        text = json.dumps(snapshot.to_dict())
        assert json.loads(text)["agent_id"] == "agent-1"


class TestProtocolVersionValidation:
    def test_missing_protocol_version_is_rejected(self):
        with pytest.raises(ProtocolError):
            AgentSnapshot.from_dict({"agent_id": "a", "host_key": "h", "generated_at": T0.isoformat(),
                                      "agent_version": "0.1.0", "applications": [], "observations": []})

    def test_non_integer_protocol_version_is_rejected(self):
        with pytest.raises(ProtocolError):
            AgentSnapshot.from_dict({
                "protocol_version": "1", "agent_id": "a", "host_key": "h", "generated_at": T0.isoformat(),
                "agent_version": "0.1.0", "applications": [], "observations": [],
            })


class TestMalformedSnapshot:
    def test_missing_required_field_raises_protocol_error(self):
        with pytest.raises(ProtocolError):
            AgentSnapshot.from_dict({
                "protocol_version": PROTOCOL_VERSION, "host_key": "h", "generated_at": T0.isoformat(),
                "agent_version": "0.1.0", "applications": [], "observations": [],
            })

    def test_naive_datetime_is_rejected(self):
        with pytest.raises(ProtocolError):
            AgentSnapshot.from_dict({
                "protocol_version": PROTOCOL_VERSION, "agent_id": "a", "host_key": "h",
                "generated_at": "2026-01-01T00:00:00", "agent_version": "0.1.0",
                "applications": [], "observations": [],
            })

    def test_applications_must_be_a_list(self):
        with pytest.raises(ProtocolError):
            AgentSnapshot.from_dict({
                "protocol_version": PROTOCOL_VERSION, "agent_id": "a", "host_key": "h",
                "generated_at": T0.isoformat(), "agent_version": "0.1.0",
                "applications": "not-a-list", "observations": [],
            })

    def test_body_not_a_dict_is_rejected(self):
        with pytest.raises(ProtocolError):
            AgentSnapshot.from_dict([])  # type: ignore[arg-type]


class TestEvidenceCandidateWireValidation:
    def test_round_trips(self):
        wire = _make_evidence_wire()
        restored = EvidenceCandidateWire.from_dict(wire.to_dict())
        assert restored == wire

    def test_unknown_category_is_rejected(self):
        data = _make_evidence_wire().to_dict()
        data["category"] = "not_a_real_category"
        with pytest.raises(ProtocolError):
            EvidenceCandidateWire.from_dict(data)

    def test_unknown_source_type_is_rejected(self):
        data = _make_evidence_wire().to_dict()
        data["source_type"] = "docker_exec"
        with pytest.raises(ProtocolError):
            EvidenceCandidateWire.from_dict(data)

    def test_negative_count_is_rejected(self):
        data = _make_evidence_wire().to_dict()
        data["count"] = -1
        with pytest.raises(ProtocolError):
            EvidenceCandidateWire.from_dict(data)

    def test_oversized_sample_is_rejected(self):
        data = _make_evidence_wire().to_dict()
        data["sample"] = "x" * (MAX_SAMPLE_LENGTH + 1)
        with pytest.raises(ProtocolError):
            EvidenceCandidateWire.from_dict(data)

    def test_to_signal_candidate_carries_no_routing_fields(self):
        # SignalCandidate has no application_key/container_id/source_*
        # of its own -- confirms the routing fields stay only on the
        # wire type, exactly matching argus.evidence.aggregator's shape.
        candidate = _make_evidence_wire().to_signal_candidate()
        assert not hasattr(candidate, "application_key")
        assert not hasattr(candidate, "container_id")
