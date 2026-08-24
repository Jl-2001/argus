"""Tests for argus.evidence.collector: per-container log collection,
cursoring/dedup, per-container failure isolation, and Docker-fact
evidence (restart_count / docker_health)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import docker.errors
import pytest

from argus.collectors.docker_client import DockerClient
from argus.domain.models import EvidenceCategory, EvidenceSeverity
from argus.evidence.collector import (
    DEFAULT_EVIDENCE_LIMITS,
    EvidenceCollectionLimits,
    collect_evidence_for_container,
    docker_fact_evidence,
)

UTC = timezone.utc
T0 = datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Fake Docker SDK plumbing
# --------------------------------------------------------------------------


class _FakeContainer:
    def __init__(self, id, log_bytes=b"", log_error=None):
        self.id = id
        self.attrs = {}
        self._log_bytes = log_bytes
        self._log_error = log_error
        self.logs_calls: list[dict] = []

    def logs(self, **kwargs):
        self.logs_calls.append(kwargs)
        if self._log_error is not None:
            raise self._log_error
        return self._log_bytes


class _FakeContainersAPI:
    def __init__(self, by_id):
        self._by_id = by_id

    def list(self, all=True):
        return list(self._by_id.values())

    def get(self, cid):
        try:
            return self._by_id[cid]
        except KeyError:
            raise docker.errors.NotFound(f"no such container: {cid}")


class _FakeSDKClient:
    def __init__(self, by_id):
        self.containers = _FakeContainersAPI(by_id)


def _ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")


def make_client(container_id: str, lines: list[tuple[datetime, str]], log_error=None) -> DockerClient:
    body = "".join(f"{_ts(dt)} {text}\n" for dt, text in lines).encode("utf-8")
    container = _FakeContainer(container_id, log_bytes=body, log_error=log_error)
    return DockerClient(client=_FakeSDKClient({container_id: container}))


# --------------------------------------------------------------------------
# Basic collection + classification + aggregation wiring
# --------------------------------------------------------------------------


class TestCollectEvidenceForContainer:
    def test_matching_lines_become_a_candidate(self):
        client = make_client(
            "c1",
            [
                (T0, "service ready"),  # innocent -- no evidence
                (T0 + timedelta(seconds=1), "connection timeout after 30s"),
                (T0 + timedelta(seconds=2), "connection timeout after 45s"),
            ],
        )
        result = collect_evidence_for_container(client, "c1", cursor_after=None, tick_at=T0 + timedelta(minutes=1))
        assert result.error is None
        assert len(result.candidates) == 1
        assert result.candidates[0].category is EvidenceCategory.DB_CONNECTION_TIMEOUT
        assert result.candidates[0].count == 2
        assert result.lines_read == 3  # all 3 lines counted as "read", only 2 became evidence

    def test_no_matching_lines_produces_no_candidates_but_still_advances_cursor(self):
        client = make_client("c1", [(T0, "all good"), (T0 + timedelta(seconds=1), "still good")])
        result = collect_evidence_for_container(client, "c1", cursor_after=None, tick_at=T0 + timedelta(minutes=1))
        assert result.candidates == ()
        assert result.new_cursor_at == T0 + timedelta(seconds=1)

    def test_samples_are_redacted_before_being_returned(self):
        client = make_client(
            "c1", [(T0, "FATAL error, Authorization: Bearer abc123secrettoken")]
        )
        result = collect_evidence_for_container(client, "c1", cursor_after=None, tick_at=T0 + timedelta(minutes=1))
        assert len(result.candidates) == 1
        assert "abc123secrettoken" not in result.candidates[0].sample
        assert "[REDACTED]" in result.candidates[0].sample


# --------------------------------------------------------------------------
# Cursoring / dedup
# --------------------------------------------------------------------------


class TestCursorDedup:
    def test_lines_at_or_before_cursor_are_never_recounted(self):
        client = make_client(
            "c1",
            [
                (T0, "connection timeout after 30s"),
                (T0 + timedelta(seconds=5), "connection timeout after 30s"),
            ],
        )
        # cursor_after == the first line's own timestamp -- only the
        # second line is genuinely new.
        result = collect_evidence_for_container(
            client, "c1", cursor_after=T0, tick_at=T0 + timedelta(minutes=1)
        )
        assert result.lines_read == 1
        assert result.candidates[0].count == 1

    def test_cursor_exactly_equal_to_last_line_reads_nothing_new(self):
        client = make_client("c1", [(T0, "connection timeout")])
        result = collect_evidence_for_container(client, "c1", cursor_after=T0, tick_at=T0 + timedelta(minutes=1))
        assert result.lines_read == 0
        assert result.candidates == ()
        assert result.new_cursor_at is None

    def test_new_cursor_at_is_the_last_new_lines_own_timestamp(self):
        client = make_client(
            "c1",
            [
                (T0, "connection timeout"),
                (T0 + timedelta(seconds=3), "connection timeout"),
                (T0 + timedelta(seconds=7), "connection timeout"),
            ],
        )
        result = collect_evidence_for_container(client, "c1", cursor_after=None, tick_at=T0 + timedelta(minutes=1))
        assert result.new_cursor_at == T0 + timedelta(seconds=7)

    def test_first_ever_read_uses_initial_lookback_not_cursor(self):
        seen_since = {}

        class _RecordingContainer(_FakeContainer):
            def logs(self, **kwargs):
                seen_since["since"] = kwargs.get("since")
                return super().logs(**kwargs)

        body = f"{_ts(T0)} hello\n".encode("utf-8")
        container = _RecordingContainer("c1", log_bytes=body)
        client = DockerClient(client=_FakeSDKClient({"c1": container}))

        tick_at = T0 + timedelta(minutes=10)
        limits = EvidenceCollectionLimits(initial_lookback_seconds=120)
        collect_evidence_for_container(client, "c1", cursor_after=None, tick_at=tick_at, limits=limits)

        assert seen_since["since"] == tick_at - timedelta(seconds=120)


# --------------------------------------------------------------------------
# Per-container failure isolation
# --------------------------------------------------------------------------


class TestContainerFailureIsolation:
    def test_docker_unavailable_is_caught_and_reported_as_error_not_raised(self):
        client = make_client("c1", [], log_error=docker.errors.DockerException("daemon unreachable"))
        result = collect_evidence_for_container(client, "c1", cursor_after=None, tick_at=T0)
        assert result.candidates == ()
        assert result.error is not None
        assert "daemon unreachable" in result.error

    def test_vanished_container_is_caught_and_reported_as_error_not_raised(self):
        client = DockerClient(client=_FakeSDKClient({}))  # "c1" doesn't exist
        result = collect_evidence_for_container(client, "c1", cursor_after=None, tick_at=T0)
        assert result.candidates == ()
        assert result.error is not None


# --------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------


class TestBounds:
    def test_tail_is_passed_through_to_docker(self):
        seen = {}

        class _RecordingContainer(_FakeContainer):
            def logs(self, **kwargs):
                seen["tail"] = kwargs.get("tail")
                return super().logs(**kwargs)

        container = _RecordingContainer("c1", log_bytes=b"")
        client = DockerClient(client=_FakeSDKClient({"c1": container}))
        limits = EvidenceCollectionLimits(max_lines_per_container=42)

        collect_evidence_for_container(client, "c1", cursor_after=None, tick_at=T0, limits=limits)
        assert seen["tail"] == 42

    def test_sample_is_truncated_to_max_sample_length(self):
        long_text = "connection timeout " + ("x" * 1000)
        client = make_client("c1", [(T0, long_text)])
        limits = EvidenceCollectionLimits(max_sample_length=50)
        result = collect_evidence_for_container(client, "c1", cursor_after=None, tick_at=T0, limits=limits)
        assert len(result.candidates[0].sample) <= 50

    def test_byte_budget_stops_reading_further_lines(self):
        lines = [(T0 + timedelta(seconds=i), "connection timeout after 30s") for i in range(20)]
        client = make_client("c1", lines)
        limits = EvidenceCollectionLimits(max_bytes_per_container=100)
        result = collect_evidence_for_container(client, "c1", cursor_after=None, tick_at=T0 + timedelta(minutes=1), limits=limits)
        assert result.lines_read < 20


# --------------------------------------------------------------------------
# Malformed / unparseable lines
# --------------------------------------------------------------------------


class TestMalformedLines:
    def test_line_without_a_docker_timestamp_prefix_is_skipped_not_fatal(self):
        raw = b"not a valid docker log line at all\n" + f"{_ts(T0)} connection timeout\n".encode("utf-8")
        client = DockerClient(client=_FakeSDKClient({"c1": _FakeContainer("c1", log_bytes=raw)}))
        result = collect_evidence_for_container(client, "c1", cursor_after=None, tick_at=T0 + timedelta(minutes=1))
        assert result.error is None
        assert len(result.candidates) == 1


# --------------------------------------------------------------------------
# Docker-fact evidence
# --------------------------------------------------------------------------


class TestDockerFactEvidence:
    def test_restart_count_increase_produces_container_restart_evidence(self):
        candidates = docker_fact_evidence(
            observed_at=T0, restart_count_before=2, restart_count_after=5, docker_health_is_unhealthy=False
        )
        assert len(candidates) == 1
        assert candidates[0].category is EvidenceCategory.CONTAINER_RESTART
        assert "2" in candidates[0].sample and "5" in candidates[0].sample

    def test_no_prior_observation_never_produces_restart_evidence(self):
        candidates = docker_fact_evidence(
            observed_at=T0, restart_count_before=None, restart_count_after=5, docker_health_is_unhealthy=False
        )
        assert candidates == []

    def test_unchanged_restart_count_produces_no_evidence(self):
        candidates = docker_fact_evidence(
            observed_at=T0, restart_count_before=3, restart_count_after=3, docker_health_is_unhealthy=False
        )
        assert candidates == []

    def test_unhealthy_docker_health_produces_container_unhealthy_evidence(self):
        candidates = docker_fact_evidence(
            observed_at=T0, restart_count_before=0, restart_count_after=0, docker_health_is_unhealthy=True
        )
        assert len(candidates) == 1
        assert candidates[0].category is EvidenceCategory.CONTAINER_UNHEALTHY
        assert candidates[0].severity is EvidenceSeverity.HIGH

    def test_both_restart_and_unhealthy_produce_two_separate_candidates(self):
        candidates = docker_fact_evidence(
            observed_at=T0, restart_count_before=1, restart_count_after=2, docker_health_is_unhealthy=True
        )
        categories = {c.category for c in candidates}
        assert categories == {EvidenceCategory.CONTAINER_RESTART, EvidenceCategory.CONTAINER_UNHEALTHY}
