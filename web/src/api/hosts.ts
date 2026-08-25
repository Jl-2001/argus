import { apiGet } from "./client";
import type { HostDetailResponse, HostSummaryResponse } from "./types";

export function listHosts(): Promise<HostSummaryResponse[]> {
  return apiGet<HostSummaryResponse[]>("/api/v1/hosts");
}

export function getHost(hostKey: string): Promise<HostDetailResponse> {
  return apiGet<HostDetailResponse>(`/api/v1/hosts/${encodeURIComponent(hostKey)}`);
}
