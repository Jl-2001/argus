import { apiGet } from "./client";
import type { IncidentDetailResponse, IncidentsListResponse } from "./types";

export function listIncidents(status?: "open" | "all"): Promise<IncidentsListResponse> {
  return apiGet<IncidentsListResponse>("/api/v1/incidents", { status });
}

export function getIncident(id: number): Promise<IncidentDetailResponse> {
  return apiGet<IncidentDetailResponse>(`/api/v1/incidents/${id}`);
}
