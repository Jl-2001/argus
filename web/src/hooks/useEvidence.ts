import { useQuery } from "@tanstack/react-query";
import {
  getIncidentBundle, getIncidentEvidence, getIncidentExplanations, getLatestIncidentExplanation,
} from "@/api/evidence";

/** Polled every 60s -- fallback only; `evidence.updated` over SSE
 * (`src/realtime/`) is what actually surfaces new evidence promptly
 * while an incident is being viewed. */
export function useIncidentEvidence(incidentId: number | undefined, limit?: number) {
  return useQuery({
    queryKey: ["incidents", incidentId, "evidence", { limit }],
    queryFn: () => getIncidentEvidence(incidentId!, limit),
    enabled: incidentId !== undefined,
    refetchInterval: 60000,
  });
}

/** The bundle is assembled fresh per request (deterministically) --
 * same fallback cadence as evidence, since its contents are derived
 * from the same underlying signals/transitions/observations. */
export function useIncidentBundle(incidentId: number | undefined) {
  return useQuery({
    queryKey: ["incidents", incidentId, "bundle"],
    queryFn: () => getIncidentBundle(incidentId!),
    enabled: incidentId !== undefined,
    refetchInterval: 60000,
  });
}

/** Persisted explanations only change when a (future, explicit,
 * cost-incurring) generation action runs -- polled gently (30s) as a
 * fallback; `explanation.available` over SSE is what actually surfaces
 * a freshly-generated one promptly. */
export function useIncidentExplanations(incidentId: number | undefined) {
  return useQuery({
    queryKey: ["incidents", incidentId, "explanations"],
    queryFn: () => getIncidentExplanations(incidentId!),
    enabled: incidentId !== undefined,
    refetchInterval: 30000,
  });
}

export function useLatestIncidentExplanation(incidentId: number | undefined) {
  return useQuery({
    queryKey: ["incidents", incidentId, "explanations", "latest"],
    queryFn: () => getLatestIncidentExplanation(incidentId!),
    enabled: incidentId !== undefined,
    refetchInterval: 30000,
  });
}
