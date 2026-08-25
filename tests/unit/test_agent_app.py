"""Milestone 16 -- `argus.agent.app`: the collect -> POST -> wait ->
repeat loop and its backoff, entirely with injected clock/sleep/stop
(no real time, no real network, no real Docker)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from argus.agent.app import compute_agent_backoff, run_agent_forever
from argus.agent.client import IngestOutcome
from argus.agent.config import AgentConfig
from argus.agent.snapshot import AgentSnapshotResult
from argus.collectors.docker_client import DockerUnavailableError

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _config(**overrides) -> AgentConfig:
    defaults = dict(
        control_plane_url="https://mac.example", agent_id="agent-1", agent_token="t",
        host_key="dell", host_name="Dell", poll_interval_seconds=15.0,
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


class _FakeCollector:
    def __init__(self, *, raise_docker_unavailable=False, raise_unexpected=False):
        self.calls = 0
        self._raise_docker_unavailable = raise_docker_unavailable
        self._raise_unexpected = raise_unexpected

    def collect_snapshot(self, *, now):
        self.calls += 1
        if self._raise_docker_unavailable:
            raise DockerUnavailableError("daemon down")
        if self._raise_unexpected:
            raise RuntimeError("boom")
        return AgentSnapshotResult(applications=(), observations=(), evidence_candidates=(), skipped=0)


class _ScriptedClock:
    def __init__(self, start: datetime):
        self._t = start

    def __call__(self) -> datetime:
        return self._t


class TestComputeAgentBackoff:
    def test_zero_failures_means_no_backoff(self):
        assert compute_agent_backoff(0, 15.0) == 0.0

    def test_doubles_each_failure(self):
        assert compute_agent_backoff(1, 15.0) == 15.0
        assert compute_agent_backoff(2, 15.0) == 30.0
        assert compute_agent_backoff(3, 15.0) == 60.0

    def test_capped(self):
        assert compute_agent_backoff(20, 15.0) == 240.0


class TestRunAgentForever:
    def test_successful_poll_sleeps_the_normal_interval(self, monkeypatch):
        collector = _FakeCollector()
        sleeps = []
        monkeypatch.setattr("argus.agent.app.post_snapshot", lambda **kwargs: IngestOutcome(True, 200, None))

        run_agent_forever(
            _config(poll_interval_seconds=15.0), collector, clock=lambda: T0,
            sleep=sleeps.append, stop_after_n_polls=1,
        )
        assert sleeps == [15.0]

    def test_failed_post_backs_off_and_resets_on_next_success(self, monkeypatch):
        collector = _FakeCollector()
        sleeps = []
        outcomes = iter([
            IngestOutcome(False, 503, "control plane unavailable"),
            IngestOutcome(False, 503, "control plane unavailable"),
            IngestOutcome(True, 200, None),
        ])
        monkeypatch.setattr("argus.agent.app.post_snapshot", lambda **kwargs: next(outcomes))

        run_agent_forever(
            _config(poll_interval_seconds=15.0), collector, clock=lambda: T0,
            sleep=sleeps.append, stop_after_n_polls=3,
        )
        assert sleeps == [15.0, 30.0, 15.0]

    def test_docker_unavailable_is_survived_not_raised(self, monkeypatch):
        collector = _FakeCollector(raise_docker_unavailable=True)
        sleeps = []
        monkeypatch.setattr("argus.agent.app.post_snapshot", lambda **kwargs: IngestOutcome(True, 200, None))

        # Must not raise -- the loop keeps running to the requested poll count.
        run_agent_forever(
            _config(poll_interval_seconds=15.0), collector, clock=lambda: T0,
            sleep=sleeps.append, stop_after_n_polls=2,
        )
        assert collector.calls == 2
        assert len(sleeps) == 2

    def test_unexpected_exception_is_survived_not_raised(self, monkeypatch):
        collector = _FakeCollector(raise_unexpected=True)
        sleeps = []
        monkeypatch.setattr("argus.agent.app.post_snapshot", lambda **kwargs: IngestOutcome(True, 200, None))

        run_agent_forever(
            _config(poll_interval_seconds=15.0), collector, clock=lambda: T0,
            sleep=sleeps.append, stop_after_n_polls=2,
        )
        assert collector.calls == 2

    def test_never_retries_the_same_stale_payload_byte_for_byte(self, monkeypatch):
        # Every poll collects a fresh snapshot from the collector, even
        # immediately after a failure -- proves there is no held/retried
        # payload from a prior failed attempt.
        collector = _FakeCollector()
        monkeypatch.setattr("argus.agent.app.post_snapshot", lambda **kwargs: IngestOutcome(False, 503, "down"))

        run_agent_forever(
            _config(poll_interval_seconds=15.0), collector, clock=lambda: T0,
            sleep=lambda s: None, stop_after_n_polls=3,
        )
        assert collector.calls == 3

    def test_stop_callable_ends_the_loop(self, monkeypatch):
        collector = _FakeCollector()
        monkeypatch.setattr("argus.agent.app.post_snapshot", lambda **kwargs: IngestOutcome(True, 200, None))
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 2

        run_agent_forever(_config(), collector, clock=lambda: T0, sleep=lambda s: None, stop=stop)
        assert collector.calls == 2

    def test_generated_at_uses_the_injected_clock(self, monkeypatch):
        captured = {}

        def fake_post(**kwargs):
            captured["snapshot"] = kwargs["snapshot"]
            return IngestOutcome(True, 200, None)

        monkeypatch.setattr("argus.agent.app.post_snapshot", fake_post)
        collector = _FakeCollector()
        run_agent_forever(
            _config(), collector, clock=lambda: T0, sleep=lambda s: None, stop_after_n_polls=1,
        )
        assert captured["snapshot"].generated_at == T0
