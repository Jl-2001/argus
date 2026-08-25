/**
 * TypeScript types mirroring the Argus FastAPI read API's response
 * schemas -- one field, one shape, exactly as `argus.api.models`
 * defines them (verified against a live `app.openapi()` dump; nothing
 * here is invented). Manually typed rather than code-generated: the
 * surface is small (11 endpoints) and stable, and a generation
 * pipeline would be more machinery than this milestone needs -- see
 * the completion report for that decision.
 *
 * Every timestamp field is the string type `IsoTimestamp` (a plain
 * `string`, nullable where the backend allows it) -- already-formatted
 * UTC ISO 8601 from the backend, never parsed/mutated here except for
 * *display* (see `src/lib/format.ts`).
 */

export type IsoTimestamp = string;

// --------------------------------------------------------------------------
// System
// --------------------------------------------------------------------------

export interface CollectorStatusResponse {
  status: string; // "NEVER_RUN" | "STALE" | "FAILING" | "HEALTHY"
  last_tick_at: IsoTimestamp | null;
  last_success_at: IsoTimestamp | null;
  consecutive_failures: number;
  last_error: string | null;
}

export interface ApplicationSummaryResponse {
  key: string;
  name: string;
  status: string;
  services: number;
  containers: number;
  last_seen_at: IsoTimestamp | null;
  host_key: string;
  host_name: string;
}

export interface SystemStatusResponse {
  collector: CollectorStatusResponse;
  applications: ApplicationSummaryResponse[];
  open_incidents: number;
}

export interface DoctorCheckResponse {
  name: string;
  status: string; // "PASS" | "WARN" | "FAIL" | "SKIP"
  message: string | null;
}

export interface DoctorResponse {
  operational: boolean;
  checks: DoctorCheckResponse[];
}

// --------------------------------------------------------------------------
// Applications
// --------------------------------------------------------------------------

export interface PortResponse {
  container_port: number;
  protocol: string;
  host_binding: string | null;
}

export interface ContainerDetailResponse {
  name: string;
  docker_state: string;
  docker_health: string | null;
  restart_count: number;
  ports: PortResponse[];
}

export interface ServiceDetailResponse {
  compose_service: string | null;
  name: string;
  status: string;
  container: ContainerDetailResponse | null;
}

export interface OpenIncidentBriefResponse {
  id: number;
  status: string;
  opened_at: IsoTimestamp | null;
  closed_at: IsoTimestamp | null;
  opening_status: string;
  worst_status: string;
}

export interface ApplicationDetailResponse {
  key: string;
  name: string;
  status: string;
  last_seen_at: IsoTimestamp | null;
  services: ServiceDetailResponse[];
  open_incident: OpenIncidentBriefResponse | null;
  host_key: string;
  host_name: string;
}

export interface TransitionResponse {
  occurred_at: IsoTimestamp | null;
  scope: string; // "application" | "service" | "container"
  label: string;
  from_status: string | null;
  to_status: string;
}

export interface ApplicationHistoryResponse {
  application: string;
  since: IsoTimestamp | null;
  transitions: TransitionResponse[];
}

// --------------------------------------------------------------------------
// Incidents
// --------------------------------------------------------------------------

export interface IncidentResponse {
  id: number;
  application: string;
  application_key: string;
  status: string; // "open" | "resolved"
  opened_at: IsoTimestamp | null;
  closed_at: IsoTimestamp | null;
  opening_status: string;
  worst_status: string;
  failure_signature: string;
}

export interface IncidentsListResponse {
  incidents: IncidentResponse[];
}

export interface IncidentDetailResponse {
  id: number;
  application_key: string;
  application_name: string;
  failure_signature: string;
  status: string;
  opening_status: string;
  worst_status: string;
  opened_at: IsoTimestamp | null;
  closed_at: IsoTimestamp | null;
  evidence_count: number;
  explanation_count: number;
  has_cached_explanation: boolean;
}

// --------------------------------------------------------------------------
// Evidence
// --------------------------------------------------------------------------

export interface EvidenceItemResponse {
  category: string;
  severity: string; // "info" | "warning" | "high" | "critical"
  count: number;
  first_seen_at: IsoTimestamp | null;
  last_seen_at: IsoTimestamp | null;
  sample: string;
  source: string;
  source_type: string;
}

export interface EvidenceResponse {
  incident_id: number;
  evidence: EvidenceItemResponse[];
}

// --------------------------------------------------------------------------
// Evidence bundle
// --------------------------------------------------------------------------

export interface BundleContainerResponse {
  container_id: string;
  name: string;
  image: string;
}

export interface BundleServiceResponse {
  id: number;
  compose_service: string | null;
  name: string;
  containers: BundleContainerResponse[];
}

export interface BundleApplicationResponse {
  key: string;
  name: string;
  services: BundleServiceResponse[];
}

export interface BundleIncidentResponse {
  reference: string;
  incident_id: number;
  status: string;
  opened_at: IsoTimestamp | null;
  closed_at: IsoTimestamp | null;
  opening_status: string;
  worst_status: string;
  failure_signature: string;
}

export interface BundleWindowResponse {
  start: IsoTimestamp | null;
  end: IsoTimestamp | null;
  incident_open: boolean;
}

export interface BundleSignalResponse {
  reference: string;
  source_id: number;
  category: string;
  severity: string;
  count: number;
  first_seen_at: IsoTimestamp | null;
  last_seen_at: IsoTimestamp | null;
  sample: string;
  source_type: string;
  source_ref: string;
  container_id: string;
  source_label: string;
}

export interface BundleTransitionResponse {
  reference: string;
  source_id: number;
  scope: string;
  label: string;
  from_status: string | null;
  to_status: string;
  occurred_at: IsoTimestamp | null;
}

export interface BundleObservationResponse {
  reference: string;
  source_id: number;
  container_id: string;
  source_label: string;
  observed_at: IsoTimestamp | null;
  docker_state: string;
  docker_health: string | null;
  restart_count: number;
  derived_status: string;
  sampling_reason: string;
  related_transition_reference: string;
}

export interface BundleTimelineEntryResponse {
  timestamp: IsoTimestamp | null;
  reference: string;
  entry_type: string; // "log_signal" | "health_transition" | "observation"
  entity: string;
  facts: string;
}

export interface BundleMetadataResponse {
  generated_at: IsoTimestamp | null;
  window_start: IsoTimestamp | null;
  window_end: IsoTimestamp | null;
  assembler_version: string;
  truncated: boolean;
  omitted_counts: Record<string, number>;
  evidence_subsystem_status: string;
  fingerprint: string;
}

export interface EvidenceBundleResponse {
  incident: BundleIncidentResponse;
  application: BundleApplicationResponse;
  window: BundleWindowResponse;
  timeline: BundleTimelineEntryResponse[];
  signals: BundleSignalResponse[];
  transitions: BundleTransitionResponse[];
  observations: BundleObservationResponse[];
  metadata: BundleMetadataResponse;
}

// --------------------------------------------------------------------------
// Explanations
// --------------------------------------------------------------------------

export interface ExplanationClaimResponse {
  text: string;
  evidence_references: string[];
}

export interface RecommendationResponse {
  category: string;
  explanation: string | null;
}

export interface ExplanationBodyResponse {
  incident_id: number;
  summary: string;
  root_cause_claim: ExplanationClaimResponse | null;
  supporting_claims: ExplanationClaimResponse[];
  confidence: string; // "low" | "medium" | "high"
  recommendation: RecommendationResponse | null;
  caveats: string[];
}

export interface UsageResponse {
  input_tokens: number | null;
  output_tokens: number | null;
}

export interface ExplanationResponse {
  id: number;
  incident_id: number;
  provider: string; // "anthropic" | "gemini"
  model: string;
  prompt_version: string;
  bundle_fingerprint: string;
  created_at: IsoTimestamp | null;
  usage: UsageResponse | null;
  explanation: ExplanationBodyResponse;
}

export interface ExplanationsListResponse {
  incident_id: number;
  explanations: ExplanationResponse[];
}

// --------------------------------------------------------------------------
// Hosts -- Milestone 16
// --------------------------------------------------------------------------

export interface HostSummaryResponse {
  host_key: string;
  display_name: string;
  kind: string; // "local" | "agent"
  status: string; // "ONLINE" | "STALE" | "OFFLINE"
  last_seen_at: IsoTimestamp | null;
  agent_version: string | null;
  application_count: number;
}

export interface HostDetailResponse extends HostSummaryResponse {
  first_seen_at: IsoTimestamp | null;
  applications: ApplicationSummaryResponse[];
}

// --------------------------------------------------------------------------
// Errors
// --------------------------------------------------------------------------

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
  };
}
