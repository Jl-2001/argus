"""The closed set of realtime event types, and the small constants the
rest of ``argus.realtime``/``argus.api.routes.events`` share.

Every event ever written has a ``type`` from this enum -- nothing in
Argus inserts an arbitrary event-type string, matching the same
closed-vocabulary discipline ``argus.domain.models.HealthStatus`` and
``argus.doctor.checks.CheckStatus`` already use.
"""

from __future__ import annotations

from enum import Enum

__all__ = ["EventType", "SCHEMA_VERSION", "RETENTION_KEEP_LAST", "HEARTBEAT_INTERVAL_SECONDS", "POLL_INTERVAL_SECONDS"]


class EventType(str, Enum):
    COLLECTOR_TICK = "collector.tick"
    APPLICATION_STATUS_CHANGED = "application.status_changed"
    SERVICE_STATUS_CHANGED = "service.status_changed"
    CONTAINER_STATUS_CHANGED = "container.status_changed"
    INCIDENT_OPENED = "incident.opened"
    INCIDENT_UPDATED = "incident.updated"
    INCIDENT_RESOLVED = "incident.resolved"
    EVIDENCE_UPDATED = "evidence.updated"
    EVIDENCE_HEALTH_CHANGED = "evidence.health_changed"
    EXPLANATION_AVAILABLE = "explanation.available"


#: Bumped only if an event payload's *shape* changes incompatibly.
#: Carried in every payload (`argus.realtime.emitter._emit`) so a future
#: frontend/voice consumer can tell which shape it's looking at without
#: guessing from field presence.
SCHEMA_VERSION = 1

#: Retention policy: keep only the most recent N events (see
#: `Repository.prune_realtime_events`, called opportunistically after
#: every insert). A plain row count, not a time window -- the simplest
#: policy that still bounds table growth at homelab scale, per the
#: milestone's own "do not overbuild" guidance.
RETENTION_KEEP_LAST = 10_000

#: How often GET /api/v1/events sends a `: heartbeat` comment on an
#: otherwise-idle connection, so intermediary proxies/browsers don't
#: time it out. Transport-level only -- never a `realtime_events` row.
HEARTBEAT_INTERVAL_SECONDS = 15.0

#: How often the SSE endpoint polls `realtime_events` for new rows.
#: SQLite has no native push/notify mechanism, so this short poll is
#: the whole "server push" mechanism -- cheap and bounded at this scale
#: (a single indexed `id > ?` query), not a filesystem watcher or a
#: second background thread.
POLL_INTERVAL_SECONDS = 0.5
