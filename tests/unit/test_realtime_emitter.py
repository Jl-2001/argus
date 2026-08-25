"""Milestone 15 -- `argus.realtime.emitter`: correct event types/payload
shapes, failure isolation (a broken realtime_events write never raises
out of an emitter call), and sanitization (no evidence sample, secret,
label, or free-text explanation content ever reaches a payload)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from argus.domain.models import HealthStatus
from argus.incidents.engine import (
    IncidentOpened,
    IncidentProcessingResult,
    IncidentResolved,
    IncidentUpdated,
    TransitionOccurred,
)
from argus.realtime import emitter
from argus.realtime.events import EventType
from argus.store.database import open_database
from argus.store.repository import Repository

UTC = timezone.utc
NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)


def _repo(tmp_path) -> Repository:
    conn = open_database(tmp_path / "a.db")
    return Repository(conn)


def _only_event(repo: Repository):
    events = repo.list_realtime_events_since(after_id=0)
    assert len(events) == 1
    return events[0]


class TestCollectorTick:
    def test_success_payload(self, tmp_path):
        repo = _repo(tmp_path)
        emitter.emit_collector_tick(repo, success=True, tick_at=NOW, applications=3, observations=7, now=NOW)
        event = _only_event(repo)
        assert event.event_type == EventType.COLLECTOR_TICK.value
        payload = json.loads(event.payload_json)
        assert payload == {"schema_version": 1, "success": True, "tick_at": NOW.isoformat(), "applications": 3, "observations": 7}

    def test_failure_payload(self, tmp_path):
        repo = _repo(tmp_path)
        emitter.emit_collector_tick(repo, success=False, tick_at=NOW, applications=0, observations=0, now=NOW)
        payload = json.loads(_only_event(repo).payload_json)
        assert payload["success"] is False


class TestIncidentProcessingEvents:
    def test_transitions_map_to_the_correct_scoped_event_type(self, tmp_path):
        repo = _repo(tmp_path)
        result = IncidentProcessingResult(
            transitions_created=3, incidents_opened=0, incidents_updated=0, incidents_resolved=0,
            transitions=(
                TransitionOccurred(scope="application", scope_id=1, application_key="cnstrct", from_status=None, to_status=HealthStatus.HEALTHY, transition_id=10, occurred_at=NOW),
                TransitionOccurred(scope="service", scope_id=2, application_key="cnstrct", from_status=HealthStatus.HEALTHY, to_status=HealthStatus.UNHEALTHY, transition_id=11, occurred_at=NOW),
                TransitionOccurred(scope="container", scope_id=3, application_key="cnstrct", from_status=HealthStatus.HEALTHY, to_status=HealthStatus.UNHEALTHY, transition_id=12, occurred_at=NOW),
            ),
        )
        emitter.emit_incident_processing_events(repo, result=result, now=NOW)
        events = repo.list_realtime_events_since(after_id=0)
        assert [e.event_type for e in events] == [
            EventType.APPLICATION_STATUS_CHANGED.value, EventType.SERVICE_STATUS_CHANGED.value,
            EventType.CONTAINER_STATUS_CHANGED.value,
        ]
        app_payload = json.loads(events[0].payload_json)
        assert app_payload == {
            "schema_version": 1, "scope_id": 1, "application_key": "cnstrct",
            "from_status": None, "to_status": "HEALTHY", "transition_id": 10,
        }

    def test_incident_opened(self, tmp_path):
        repo = _repo(tmp_path)
        result = IncidentProcessingResult(
            transitions_created=0, incidents_opened=1, incidents_updated=0, incidents_resolved=0,
            opened_incidents=(IncidentOpened(incident_id=14, application_key="musipal", opening_status=HealthStatus.DEGRADED),),
        )
        emitter.emit_incident_processing_events(repo, result=result, now=NOW)
        event = _only_event(repo)
        assert event.event_type == EventType.INCIDENT_OPENED.value
        assert json.loads(event.payload_json) == {
            "schema_version": 1, "incident_id": 14, "application_key": "musipal", "opening_status": "DEGRADED",
        }

    def test_incident_escalated_emits_one_updated_event(self, tmp_path):
        repo = _repo(tmp_path)
        result = IncidentProcessingResult(
            transitions_created=0, incidents_opened=0, incidents_updated=1, incidents_resolved=0,
            updated_incidents=(IncidentUpdated(incident_id=14, application_key="musipal", worst_status=HealthStatus.UNHEALTHY),),
        )
        emitter.emit_incident_processing_events(repo, result=result, now=NOW)
        event = _only_event(repo)
        assert event.event_type == EventType.INCIDENT_UPDATED.value
        assert json.loads(event.payload_json)["worst_status"] == "UNHEALTHY"

    def test_incident_unchanged_emits_no_updated_event(self, tmp_path):
        """A real transition happened (e.g. UNHEALTHY -> DEGRADED) but it
        wasn't an escalation -- the engine itself never appends to
        `updated_incidents` for that case (see engine.py's own "nothing
        to write" branch), so an empty result here must emit nothing."""

        repo = _repo(tmp_path)
        result = IncidentProcessingResult(transitions_created=1, incidents_opened=0, incidents_updated=0, incidents_resolved=0)
        emitter.emit_incident_processing_events(repo, result=result, now=NOW)
        assert repo.list_realtime_events_since(after_id=0) == ()

    def test_incident_resolved(self, tmp_path):
        repo = _repo(tmp_path)
        result = IncidentProcessingResult(
            transitions_created=0, incidents_opened=0, incidents_updated=0, incidents_resolved=1,
            resolved_incidents=(IncidentResolved(incident_id=14, application_key="musipal"),),
        )
        emitter.emit_incident_processing_events(repo, result=result, now=NOW)
        event = _only_event(repo)
        assert event.event_type == EventType.INCIDENT_RESOLVED.value
        assert json.loads(event.payload_json) == {"schema_version": 1, "incident_id": 14, "application_key": "musipal"}


class TestEvidenceUpdated:
    def test_no_event_when_nothing_changed(self, tmp_path):
        repo = _repo(tmp_path)
        emitter.emit_evidence_updated(repo, signals_created=0, associations=0, tick_at=NOW, now=NOW)
        assert repo.list_realtime_events_since(after_id=0) == ()

    def test_event_when_signals_created(self, tmp_path):
        repo = _repo(tmp_path)
        emitter.emit_evidence_updated(repo, signals_created=3, associations=1, tick_at=NOW, now=NOW)
        payload = json.loads(_only_event(repo).payload_json)
        assert payload == {"schema_version": 1, "signals_created": 3, "associations": 1}


class TestEvidenceHealthChanged:
    def test_payload_reflects_healthy_flag(self, tmp_path):
        repo = _repo(tmp_path)
        emitter.emit_evidence_health_changed(repo, healthy=False, tick_at=NOW, now=NOW)
        assert json.loads(_only_event(repo).payload_json) == {"schema_version": 1, "healthy": False}


class TestExplanationAvailable:
    def test_payload_shape(self, tmp_path):
        repo = _repo(tmp_path)
        emitter.emit_explanation_available(
            repo, incident_id=14, provider="gemini", model="gemini-3.5-flash",
            bundle_fingerprint="abc123", now=NOW,
        )
        event = _only_event(repo)
        assert event.event_type == EventType.EXPLANATION_AVAILABLE.value
        assert json.loads(event.payload_json) == {
            "schema_version": 1, "incident_id": 14, "provider": "gemini",
            "model": "gemini-3.5-flash", "bundle_fingerprint": "abc123",
        }


class TestSanitization:
    """No emitter function accepts (or could therefore leak) an evidence
    sample, a raw log line, a Docker label, an env var, an API key, a
    system prompt, or a full AI explanation body -- every payload is
    built from a fixed, small set of named parameters, not "whatever
    the caller passed"."""

    _FORBIDDEN_SNIPPETS = ("DATABASE_URL", "sk-ant-", "AIza", "/var/run/docker.sock", "password", "SYSTEM PROMPT")

    def test_no_emitted_payload_ever_contains_forbidden_content(self, tmp_path):
        repo = _repo(tmp_path)
        emitter.emit_collector_tick(repo, success=True, tick_at=NOW, applications=1, observations=1, now=NOW)
        emitter.emit_incident_processing_events(
            repo, now=NOW,
            result=IncidentProcessingResult(
                transitions_created=1, incidents_opened=1, incidents_updated=1, incidents_resolved=1,
                transitions=(TransitionOccurred(scope="application", scope_id=1, application_key="cnstrct", from_status=HealthStatus.HEALTHY, to_status=HealthStatus.UNHEALTHY, transition_id=1, occurred_at=NOW),),
                opened_incidents=(IncidentOpened(incident_id=1, application_key="cnstrct", opening_status=HealthStatus.UNHEALTHY),),
                updated_incidents=(IncidentUpdated(incident_id=1, application_key="cnstrct", worst_status=HealthStatus.UNHEALTHY),),
                resolved_incidents=(IncidentResolved(incident_id=1, application_key="cnstrct"),),
            ),
        )
        emitter.emit_evidence_updated(repo, signals_created=1, associations=1, tick_at=NOW, now=NOW)
        emitter.emit_evidence_health_changed(repo, healthy=False, tick_at=NOW, now=NOW)
        emitter.emit_explanation_available(repo, incident_id=1, provider="anthropic", model="claude-sonnet-5", bundle_fingerprint="fp", now=NOW)

        for event in repo.list_realtime_events_since(after_id=0):
            for snippet in self._FORBIDDEN_SNIPPETS:
                assert snippet not in event.payload_json

    def test_every_payload_field_is_a_named_scalar_or_none(self, tmp_path):
        """Guards against a future change accidentally passing a whole
        object (e.g. an EvidenceBundle or IncidentExplanation) into a
        payload -- every value must be a plain JSON scalar."""

        repo = _repo(tmp_path)
        emitter.emit_explanation_available(repo, incident_id=1, provider="anthropic", model="claude-sonnet-5", bundle_fingerprint="fp", now=NOW)
        payload = json.loads(_only_event(repo).payload_json)
        for value in payload.values():
            assert isinstance(value, (str, int, float, bool)) or value is None


class TestFailureIsolation:
    def test_a_broken_realtime_events_write_never_raises(self, tmp_path):
        repo = _repo(tmp_path)

        def _boom(*args, **kwargs):
            raise RuntimeError("disk full (simulated)")

        repo.insert_realtime_event = _boom  # type: ignore[method-assign]

        # None of these may raise -- the whole point of _emit's own
        # try/except.
        emitter.emit_collector_tick(repo, success=True, tick_at=NOW, applications=1, observations=1, now=NOW)
        emitter.emit_evidence_updated(repo, signals_created=1, associations=0, tick_at=NOW, now=NOW)
        emitter.emit_explanation_available(repo, incident_id=1, provider="anthropic", model="m", bundle_fingerprint="fp", now=NOW)

    def test_a_broken_prune_never_raises_and_the_event_is_still_recorded(self, tmp_path):
        repo = _repo(tmp_path)

        def _boom(*args, **kwargs):
            raise RuntimeError("prune failed (simulated)")

        repo.prune_realtime_events = _boom  # type: ignore[method-assign]

        emitter.emit_collector_tick(repo, success=True, tick_at=NOW, applications=1, observations=1, now=NOW)
        # The insert itself must still have gone through -- only the
        # opportunistic cleanup afterward failed.
        assert len(repo.list_realtime_events_since(after_id=0)) == 1
