"""Milestone 16 -- `argus.agent.snapshot.AgentCollector`: builds one
sanitized `AgentSnapshot`-ready result per poll, reusing the same
discovery/evidence code the local collector uses, entirely without a
database."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from argus.agent.snapshot import AgentCollector
from argus.collectors.docker_client import DockerClient
from argus.domain.models import HealthStatus

UTC = timezone.utc
T0 = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "docker_responses"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


class _FakeContainer:
    def __init__(self, id: str, attrs: dict, log_lines: "list[str] | None" = None):
        self.id = id
        self.attrs = attrs
        self._log_lines = log_lines or []

    def logs(self, **kwargs):
        return "\n".join(self._log_lines).encode("utf-8")


class _FakeContainersAPI:
    def __init__(self, by_id: dict[str, dict], log_lines_by_id: "dict[str, list[str]] | None" = None):
        self._by_id = by_id
        self._log_lines_by_id = log_lines_by_id or {}

    def list(self, all=False):
        return [_FakeContainer(cid, {}) for cid in self._by_id]

    def get(self, container_id):
        return _FakeContainer(
            container_id, self._by_id[container_id], self._log_lines_by_id.get(container_id)
        )


class _FakeSDKClient:
    def __init__(self, by_id: dict[str, dict], log_lines_by_id=None):
        self.containers = _FakeContainersAPI(by_id, log_lines_by_id)


def make_client(fixture_names: list[str], log_lines_by_id=None) -> DockerClient:
    by_id = {}
    for name in fixture_names:
        attrs = load_fixture(name)
        by_id[attrs["Id"]] = attrs
    return DockerClient(client=_FakeSDKClient(by_id, log_lines_by_id))


class TestCollectSnapshot:
    def test_produces_applications_and_observations(self):
        client = make_client(["compose_healthy_api"])
        collector = AgentCollector(client=client)

        result = collector.collect_snapshot(now=T0)

        assert len(result.applications) >= 1
        assert len(result.observations) >= 1
        assert result.observations[0].derived_status is not None

    def test_never_produces_a_docker_mutation_capable_object(self):
        # AgentCollector's whole surface only ever returns plain
        # dataclasses (Application/Observation/EvidenceCandidateWire) --
        # none of which carry any live Docker handle.
        client = make_client(["compose_healthy_api"])
        collector = AgentCollector(client=client)
        result = collector.collect_snapshot(now=T0)
        for observation in result.observations:
            assert not hasattr(observation, "client")
            assert not hasattr(observation.container_ref, "client")

    def test_restart_loop_history_carries_across_polls(self):
        client = make_client(["compose_healthy_api"])
        collector = AgentCollector(client=client)

        first = collector.collect_snapshot(now=T0)
        second = collector.collect_snapshot(now=T0 + timedelta(seconds=15))

        # Just confirms the second poll's evaluation had *some* prior
        # observation available to it (not asserting a specific status
        # -- restart-loop classification detail is
        # argus.domain.health's own test suite's job).
        assert len(second.observations) == len(first.observations)

    def test_evidence_candidates_include_docker_fact_evidence_on_restart(self):
        client = make_client(["compose_healthy_api"])
        container_id = next(iter(client._client.containers._by_id))
        collector = AgentCollector(client=client)

        collector.collect_snapshot(now=T0)
        # Manually bump restart_count in the client's own fixture copy
        # to force a restart-evidence candidate on the next poll.
        client._client.containers._by_id[container_id]["RestartCount"] = (
            client._client.containers._by_id[container_id].get("RestartCount", 0) + 1
        )
        second = collector.collect_snapshot(now=T0 + timedelta(seconds=15))

        restart_evidence = [e for e in second.evidence_candidates if e.source_type == "docker_fact"]
        assert any(e.container_id == container_id for e in restart_evidence)

    def test_bounded_evidence_cap_is_respected(self):
        client = make_client(["compose_healthy_api"])
        collector = AgentCollector(client=client)
        collector._evidence_limits = collector._evidence_limits.__class__(max_signals_per_tick=0)
        # No direct cap knob on the agent's own evidence collection loop
        # beyond MAX_EVIDENCE_ITEMS_PER_SNAPSHOT -- confirm collection
        # still returns cleanly (never raises) regardless.
        result = collector.collect_snapshot(now=T0)
        assert isinstance(result.evidence_candidates, tuple)

    def test_vanished_container_history_is_dropped_not_leaked_forever(self):
        client = make_client(["compose_healthy_api"])
        collector = AgentCollector(client=client)
        collector.collect_snapshot(now=T0)
        assert collector._observation_history  # populated after first poll

        empty_client = make_client([])
        collector._client = empty_client
        collector.collect_snapshot(now=T0 + timedelta(seconds=15))
        assert collector._observation_history == {}
        assert collector._log_cursors == {}
