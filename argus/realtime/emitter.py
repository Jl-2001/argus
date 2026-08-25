"""Turns already-committed writes into `realtime_events` rows.

Two rules hold for every function in this module, without exception:

1. **Emit-after-commit only.** Every caller here (`argus.collector.loop`,
   `argus.ai.explain`) calls these functions *after* the write they
   describe has already committed -- never inside the same transaction,
   never speculatively before a write that might still fail. There is
   no "phantom" `incident.opened` for a rolled-back incident, because
   nothing here ever runs before the incident row exists.

2. **Never fail the caller.** Realtime delivery is auxiliary (see the
   milestone's own "Failure Isolation" section) -- `_emit` swallows and
   logs *any* exception a `realtime_events` write raises (a full disk,
   a locked database, anything) rather than letting it propagate. A
   collector tick that successfully persisted observations/transitions/
   incidents must never fail, retry, or roll any of that back merely
   because the auxiliary event log couldn't be written to. The dashboard
   falls back to its existing polling in that case -- see
   `src/hooks/*`'s fallback intervals on the frontend side.

Every payload is a small, explicit, whitelisted dict -- ids, keys,
statuses, timestamps, counts. Nothing here ever forwards an evidence
sample, a raw log line, a Docker label, an env var, an AI system
prompt, or a full AI explanation body; `tests/unit/test_realtime_emitter.py`
asserts this by construction (every field is named, not "whatever the
caller happened to pass").
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Mapping

from argus.incidents.engine import IncidentProcessingResult
from argus.realtime.events import RETENTION_KEEP_LAST, SCHEMA_VERSION, EventType
from argus.store.repository import Repository

__all__ = [
    "emit_collector_tick",
    "emit_incident_processing_events",
    "emit_evidence_updated",
    "emit_evidence_health_changed",
    "emit_explanation_available",
]

logger = logging.getLogger(__name__)


def _emit(
    repository: Repository, *, event_type: EventType, occurred_at: datetime, payload: Mapping[str, Any], now: datetime
) -> None:
    envelope = {"schema_version": SCHEMA_VERSION, **payload}
    try:
        repository.insert_realtime_event(
            event_type=event_type.value, occurred_at=occurred_at,
            payload_json=json.dumps(envelope, sort_keys=True), created_at=now,
        )
        # Opportunistic, not a separate scheduled job (see
        # RETENTION_KEEP_LAST's own docstring) -- cheap no-op once the
        # table is already at/under the cap.
        repository.prune_realtime_events(keep_last=RETENTION_KEEP_LAST)
    except Exception as exc:  # noqa: BLE001 -- deliberate, documented, scoped safety net
        logger.warning("realtime event %s could not be recorded (core monitoring unaffected): %s", event_type.value, exc)


def emit_collector_tick(
    repository: Repository, *, success: bool, tick_at: datetime, applications: int, observations: int, now: datetime
) -> None:
    """One `collector.tick` per successful `CollectorLoop.run_once()` --
    deliberately minimal metadata (never the full `TickResult`), so the
    dashboard's overview/system pages know to refetch collector status
    immediately without the event itself carrying that state."""

    _emit(
        repository, event_type=EventType.COLLECTOR_TICK, occurred_at=tick_at, now=now,
        payload={"success": success, "tick_at": tick_at.isoformat(), "applications": applications, "observations": observations},
    )


_TRANSITION_EVENT_TYPE = {
    "application": EventType.APPLICATION_STATUS_CHANGED,
    "service": EventType.SERVICE_STATUS_CHANGED,
    "container": EventType.CONTAINER_STATUS_CHANGED,
}


def emit_incident_processing_events(repository: Repository, *, result: IncidentProcessingResult, now: datetime) -> None:
    """One event per committed transition/incident change from one
    tick's `process_transitions_and_incidents()` result -- reads only
    the additive `transitions`/`opened_incidents`/`updated_incidents`/
    `resolved_incidents` tuples that result already carries (Milestone
    15's own extension to `IncidentProcessingResult`); by the time this
    is called, `process_transitions_and_incidents`'s own transaction has
    already committed (see `CollectorLoop.run_once`), so every event
    built here describes real, persisted state.

    `incident.updated` is only ever present in `result.updated_incidents`
    on a genuine `worst_status` escalation -- the engine itself already
    enforces "no duplicate/no-op updates" (see
    `_update_incident_lifecycle`'s own "nothing to write" branch); this
    function does not re-derive that decision, only reports it.
    """

    for transition in result.transitions:
        _emit(
            repository, event_type=_TRANSITION_EVENT_TYPE[transition.scope],
            occurred_at=transition.occurred_at, now=now,
            payload={
                "scope_id": transition.scope_id,
                "application_key": transition.application_key,
                "from_status": transition.from_status.value if transition.from_status is not None else None,
                "to_status": transition.to_status.value,
                "transition_id": transition.transition_id,
            },
        )

    for opened in result.opened_incidents:
        _emit(
            repository, event_type=EventType.INCIDENT_OPENED, occurred_at=now, now=now,
            payload={
                "incident_id": opened.incident_id, "application_key": opened.application_key,
                "opening_status": opened.opening_status.value,
            },
        )

    for updated in result.updated_incidents:
        _emit(
            repository, event_type=EventType.INCIDENT_UPDATED, occurred_at=now, now=now,
            payload={
                "incident_id": updated.incident_id, "application_key": updated.application_key,
                "worst_status": updated.worst_status.value,
            },
        )

    for resolved in result.resolved_incidents:
        _emit(
            repository, event_type=EventType.INCIDENT_RESOLVED, occurred_at=now, now=now,
            payload={"incident_id": resolved.incident_id, "application_key": resolved.application_key},
        )


def emit_evidence_updated(
    repository: Repository, *, signals_created: int, associations: int, tick_at: datetime, now: datetime
) -> None:
    """One tick-scoped aggregate event, only when something actually
    happened (`signals_created` or `associations` > 0) -- never one
    event per log line (see the milestone's own "Evidence Event"
    section: "avoid one SSE event per individual log line").

    Deliberately tick-scoped, not per-application/per-incident: the
    current evidence pipeline (`CollectorLoop._collect_and_persist_evidence`/
    `_associate_evidence`) only ever returns whole-tick totals, not a
    per-application breakdown -- attributing this event to a specific
    application/incident would mean either fabricating that
    attribution or a materially larger change to the evidence
    subsystem's own return shape than this milestone's "do not
    redesign working layers unless necessary" scope allows. The
    frontend compensates with a deliberately broad invalidation (see
    `web/src/realtime/invalidation.ts`) rather than a precise one.
    """

    if signals_created == 0 and associations == 0:
        return
    _emit(
        repository, event_type=EventType.EVIDENCE_UPDATED, occurred_at=tick_at, now=now,
        payload={"signals_created": signals_created, "associations": associations},
    )


def emit_evidence_health_changed(repository: Repository, *, healthy: bool, tick_at: datetime, now: datetime) -> None:
    """Emitted only on an actual healthy<->degraded transition of the
    *evidence subsystem itself* (see `CollectorLoop`'s own before/after
    comparison of `consecutive_evidence_failures`) -- never once per
    tick merely because the subsystem is still in whatever state it was
    already in."""

    _emit(
        repository, event_type=EventType.EVIDENCE_HEALTH_CHANGED, occurred_at=tick_at, now=now,
        payload={"healthy": healthy},
    )


def emit_explanation_available(
    repository: Repository, *, incident_id: int, provider: str, model: str, bundle_fingerprint: str, now: datetime
) -> None:
    """Called from `argus.ai.explain.IncidentExplanationService.explain()`
    after a freshly-generated (not cache-hit) explanation has already
    been persisted via `Repository.save_explanation`. Never triggers
    generation itself -- this only announces that a *already-persisted*
    explanation now exists, exactly the same boundary
    `argus.api.routes.explanations` already holds (read-only, no
    provider call from here)."""

    _emit(
        repository, event_type=EventType.EXPLANATION_AVAILABLE, occurred_at=now, now=now,
        payload={"incident_id": incident_id, "provider": provider, "model": model, "bundle_fingerprint": bundle_fingerprint},
    )
