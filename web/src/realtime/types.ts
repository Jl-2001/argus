/**
 * TypeScript mirror of `argus.realtime.events.EventType` and each
 * event's payload shape (verified against `argus/realtime/emitter.py`'s
 * own payload construction -- nothing here is invented). Every payload
 * carries `schema_version` (see the backend's own `SCHEMA_VERSION`
 * constant) so a future consumer can tell which shape it's looking at.
 */

export const REALTIME_EVENT_TYPES = [
  "collector.tick",
  "application.status_changed",
  "service.status_changed",
  "container.status_changed",
  "incident.opened",
  "incident.updated",
  "incident.resolved",
  "evidence.updated",
  "evidence.health_changed",
  "explanation.available",
] as const;

export type RealtimeEventType = (typeof REALTIME_EVENT_TYPES)[number];

export interface CollectorTickPayload {
  schema_version: number;
  success: boolean;
  tick_at: string;
  applications: number;
  observations: number;
}

export interface StatusChangedPayload {
  schema_version: number;
  scope_id: number;
  application_key: string;
  from_status: string | null;
  to_status: string;
  transition_id: number;
}

export interface IncidentOpenedPayload {
  schema_version: number;
  incident_id: number;
  application_key: string;
  opening_status: string;
}

export interface IncidentUpdatedPayload {
  schema_version: number;
  incident_id: number;
  application_key: string;
  worst_status: string;
}

export interface IncidentResolvedPayload {
  schema_version: number;
  incident_id: number;
  application_key: string;
}

export interface EvidenceUpdatedPayload {
  schema_version: number;
  signals_created: number;
  associations: number;
}

export interface EvidenceHealthChangedPayload {
  schema_version: number;
  healthy: boolean;
}

export interface ExplanationAvailablePayload {
  schema_version: number;
  incident_id: number;
  provider: string;
  model: string;
  bundle_fingerprint: string;
}

export interface StreamResetPayload {
  schema_version: number;
  reason: string;
}

/** SSE connection health -- deliberately distinct from Argus's own
 * collector health (`CollectorStatusResponse.status`); see
 * `src/components/layout/TopBar.tsx`, which renders both side by side
 * without conflating them. */
export type ConnectionState = "connecting" | "live" | "reconnecting" | "offline";
