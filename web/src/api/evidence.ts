import { apiGet } from "./client";
import type { EvidenceBundleResponse, EvidenceResponse, ExplanationResponse, ExplanationsListResponse } from "./types";

export function getIncidentEvidence(incidentId: number, limit?: number): Promise<EvidenceResponse> {
  return apiGet<EvidenceResponse>(`/api/v1/incidents/${incidentId}/evidence`, { limit });
}

export function getIncidentBundle(incidentId: number): Promise<EvidenceBundleResponse> {
  return apiGet<EvidenceBundleResponse>(`/api/v1/incidents/${incidentId}/bundle`);
}

export function getIncidentExplanations(incidentId: number): Promise<ExplanationsListResponse> {
  return apiGet<ExplanationsListResponse>(`/api/v1/incidents/${incidentId}/explanations`);
}

export function getLatestIncidentExplanation(incidentId: number): Promise<ExplanationResponse | null> {
  return apiGet<ExplanationResponse | null>(`/api/v1/incidents/${incidentId}/explanations/latest`);
}
