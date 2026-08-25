import type {
  ApplicationDetailResponse, ApplicationSummaryResponse, DoctorResponse, EvidenceBundleResponse,
  ExplanationResponse, ExplanationsListResponse, HostDetailResponse, HostSummaryResponse,
  IncidentDetailResponse, IncidentsListResponse, SystemStatusResponse,
} from "@/api/types";

export const fakeApplicationSummary: ApplicationSummaryResponse = {
  key: "cnstrct",
  name: "CNSTRCT",
  status: "HEALTHY",
  services: 2,
  containers: 2,
  last_seen_at: "2026-08-23T12:00:00+00:00",
  host_key: "local",
  host_name: "Local Host",
};

export const fakeUnhealthyApplicationSummary: ApplicationSummaryResponse = {
  key: "musipal",
  name: "Musipal",
  status: "UNHEALTHY",
  services: 3,
  containers: 3,
  last_seen_at: "2026-08-23T11:59:00+00:00",
  host_key: "local",
  host_name: "Local Host",
};

export const fakeSystemStatusHealthy: SystemStatusResponse = {
  collector: {
    status: "HEALTHY", last_tick_at: "2026-08-23T12:00:00+00:00", last_success_at: "2026-08-23T12:00:00+00:00",
    consecutive_failures: 0, last_error: null,
  },
  applications: [fakeApplicationSummary],
  open_incidents: 0,
};

export const fakeSystemStatusWithIncident: SystemStatusResponse = {
  ...fakeSystemStatusHealthy,
  applications: [fakeApplicationSummary, fakeUnhealthyApplicationSummary],
  open_incidents: 1,
};

export const fakeApplicationDetail: ApplicationDetailResponse = {
  key: "cnstrct",
  name: "CNSTRCT",
  status: "HEALTHY",
  last_seen_at: "2026-08-23T12:00:00+00:00",
  services: [
    {
      compose_service: "api",
      name: "api",
      status: "HEALTHY",
      container: {
        name: "cnstrct-api-1",
        docker_state: "running",
        docker_health: "healthy",
        restart_count: 0,
        ports: [{ container_port: 3000, protocol: "tcp", host_binding: "0.0.0.0:3000" }],
      },
    },
    {
      compose_service: "postgres",
      name: "postgres",
      status: "HEALTHY",
      container: {
        name: "cnstrct-postgres-1",
        docker_state: "running",
        docker_health: null,
        restart_count: 0,
        ports: [],
      },
    },
  ],
  open_incident: null,
  host_key: "local",
  host_name: "Local Host",
};

export const fakeHostLocal: HostSummaryResponse = {
  host_key: "local",
  display_name: "Local Host",
  kind: "local",
  status: "ONLINE",
  last_seen_at: "2026-08-23T12:00:00+00:00",
  agent_version: null,
  application_count: 2,
};

export const fakeHostRemote: HostSummaryResponse = {
  host_key: "dell-latitude-5400",
  display_name: "Ubuntu Dell",
  kind: "agent",
  status: "ONLINE",
  last_seen_at: "2026-08-23T11:59:50+00:00",
  agent_version: "0.1.0",
  application_count: 1,
};

export const fakeHostRemoteOffline: HostSummaryResponse = {
  ...fakeHostRemote,
  host_key: "old-server",
  display_name: "Old Server",
  status: "OFFLINE",
  last_seen_at: "2026-08-23T09:00:00+00:00",
};

export const fakeHostDetailRemote: HostDetailResponse = {
  ...fakeHostRemote,
  first_seen_at: "2026-08-01T00:00:00+00:00",
  applications: [{ ...fakeApplicationSummary, key: "dell-latitude-5400:cnstrct", host_key: "dell-latitude-5400", host_name: "Ubuntu Dell" }],
};

export const fakeIncidentsListOpen: IncidentsListResponse = {
  incidents: [
    {
      id: 14, application: "Musipal", application_key: "musipal", status: "open",
      opened_at: "2026-08-23T11:57:00+00:00", closed_at: null,
      opening_status: "DEGRADED", worst_status: "UNHEALTHY", failure_signature: "application:musipal",
    },
  ],
};

export const fakeIncidentsListResolved: IncidentsListResponse = {
  incidents: [
    {
      id: 12, application: "CNSTRCT", application_key: "cnstrct", status: "resolved",
      opened_at: "2026-08-22T10:00:00+00:00", closed_at: "2026-08-22T10:05:00+00:00",
      opening_status: "UNHEALTHY", worst_status: "UNHEALTHY", failure_signature: "application:cnstrct",
    },
  ],
};

export const fakeIncidentDetail: IncidentDetailResponse = {
  id: 14, application_key: "musipal", application_name: "Musipal", failure_signature: "application:musipal",
  status: "open", opening_status: "DEGRADED", worst_status: "UNHEALTHY",
  opened_at: "2026-08-23T11:57:00+00:00", closed_at: null,
  evidence_count: 1, explanation_count: 1, has_cached_explanation: true,
};

export const fakeIncidentDetailNoExplanation: IncidentDetailResponse = {
  ...fakeIncidentDetail, explanation_count: 0, has_cached_explanation: false,
};

export const fakeBundle: EvidenceBundleResponse = {
  incident: {
    reference: "incident:14", incident_id: 14, status: "open", opened_at: "2026-08-23T11:57:00+00:00",
    closed_at: null, opening_status: "DEGRADED", worst_status: "UNHEALTHY", failure_signature: "application:musipal",
  },
  application: {
    key: "musipal", name: "Musipal",
    services: [{ id: 1, compose_service: "api", name: "api", containers: [{ container_id: "abc123", name: "musipal-api-1", image: "musipal/api:latest" }] }],
  },
  window: { start: "2026-08-23T11:56:00+00:00", end: "2026-08-23T12:00:00+00:00", incident_open: true },
  timeline: [
    {
      timestamp: "2026-08-23T11:57:03+00:00", reference: "health_transition:18", entry_type: "health_transition",
      entity: "api", facts: "HEALTHY -> UNHEALTHY",
    },
    {
      timestamp: "2026-08-23T11:57:01+00:00", reference: "log_signal:42", entry_type: "log_signal",
      entity: "api", facts: "db_connection_timeout x27",
    },
  ],
  signals: [
    {
      reference: "log_signal:42", source_id: 42, category: "db_connection_timeout", severity: "high", count: 27,
      first_seen_at: "2026-08-23T11:57:01+00:00", last_seen_at: "2026-08-23T11:57:01+00:00",
      sample: "[REDACTED] connection timeout after 30s", source_type: "container_log", source_ref: "stdout",
      container_id: "abc123", source_label: "api",
    },
  ],
  transitions: [
    {
      reference: "health_transition:18", source_id: 18, scope: "application", label: "musipal",
      from_status: "DEGRADED", to_status: "UNHEALTHY", occurred_at: "2026-08-23T11:57:03+00:00",
    },
  ],
  observations: [],
  metadata: {
    generated_at: "2026-08-23T12:00:00+00:00", window_start: "2026-08-23T11:56:00+00:00",
    window_end: "2026-08-23T12:00:00+00:00", assembler_version: "v1", truncated: false,
    omitted_counts: { signals: 0, transitions: 0, observations: 0 }, evidence_subsystem_status: "healthy",
    fingerprint: "13e71582abcd1234ef",
  },
};

export const fakeExplanationAnthropic: ExplanationResponse = {
  id: 1, incident_id: 14, provider: "anthropic", model: "claude-sonnet-5", prompt_version: "incident-explanation-v1",
  bundle_fingerprint: "13e71582abcd1234ef", created_at: "2026-08-23T12:00:05+00:00",
  usage: { input_tokens: 5000, output_tokens: 300 },
  explanation: {
    incident_id: 14, summary: "The API became unhealthy after repeated database connection timeouts.",
    root_cause_claim: { text: "Database connections were timing out repeatedly.", evidence_references: ["log_signal:42"] },
    supporting_claims: [
      { text: "Health transitioned to UNHEALTHY right after the timeouts began.", evidence_references: ["health_transition:18"] },
    ],
    confidence: "high", recommendation: { category: "investigate_dependency", explanation: "Check database connectivity." },
    caveats: ["Evidence window may not capture the full incident."],
  },
};

export const fakeExplanationGemini: ExplanationResponse = {
  ...fakeExplanationAnthropic, id: 2, provider: "gemini", model: "gemini-3.5-flash",
};

export const fakeExplanationsList: ExplanationsListResponse = {
  incident_id: 14, explanations: [fakeExplanationAnthropic],
};

export const fakeExplanationsListMultiProvider: ExplanationsListResponse = {
  incident_id: 14, explanations: [fakeExplanationAnthropic, fakeExplanationGemini],
};

export const fakeExplanationsListEmpty: ExplanationsListResponse = { incident_id: 14, explanations: [] };

export const fakeDoctorHealthy: DoctorResponse = {
  operational: true,
  checks: [
    { name: "configuration", status: "PASS", message: null },
    { name: "database", status: "PASS", message: null },
    { name: "docker_connection", status: "PASS", message: null },
    { name: "docker_read_access", status: "PASS", message: null },
    { name: "collector_heartbeat", status: "PASS", message: null },
    { name: "remote_agents", status: "PASS", message: "0 remote agents configured" },
    { name: "clock", status: "PASS", message: null },
  ],
};

export const fakeDoctorFailing: DoctorResponse = {
  operational: false,
  checks: [
    { name: "configuration", status: "PASS", message: null },
    { name: "database", status: "FAIL", message: "database file does not exist at /fake/path/argus.db" },
    { name: "docker_connection", status: "FAIL", message: "could not reach the Docker daemon" },
    { name: "docker_read_access", status: "SKIP", message: "skipped: docker_connection failed" },
    { name: "collector_heartbeat", status: "SKIP", message: "skipped: database unavailable" },
    { name: "remote_agents", status: "SKIP", message: "skipped: database unavailable" },
    { name: "clock", status: "PASS", message: null },
  ],
};
