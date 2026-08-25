import { apiGet } from "./client";
import type { ApplicationDetailResponse, ApplicationHistoryResponse, ApplicationSummaryResponse } from "./types";

export function listApplications(status?: string): Promise<ApplicationSummaryResponse[]> {
  return apiGet<ApplicationSummaryResponse[]>("/api/v1/applications", { status });
}

export function getApplication(key: string): Promise<ApplicationDetailResponse> {
  return apiGet<ApplicationDetailResponse>(`/api/v1/applications/${encodeURIComponent(key)}`);
}

export function getApplicationHistory(key: string, since?: string): Promise<ApplicationHistoryResponse> {
  return apiGet<ApplicationHistoryResponse>(`/api/v1/applications/${encodeURIComponent(key)}/history`, { since });
}
