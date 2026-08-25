import { apiGet } from "./client";
import type { DoctorResponse, SystemStatusResponse } from "./types";

export function getSystemStatus(): Promise<SystemStatusResponse> {
  return apiGet<SystemStatusResponse>("/api/v1/system/status");
}

export function getDoctor(): Promise<DoctorResponse> {
  return apiGet<DoctorResponse>("/api/v1/system/doctor");
}
