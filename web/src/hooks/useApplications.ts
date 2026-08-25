import { useQuery } from "@tanstack/react-query";
import { getApplication, getApplicationHistory, listApplications } from "@/api/applications";

/** Polled every 30s -- fallback only; `application.status_changed`
 * over SSE (`src/realtime/`) is what actually makes this feel live.
 * See `useSystemStatus`'s own docstring for the same reasoning. */
export function useApplications(status?: string) {
  return useQuery({
    queryKey: ["applications", { status }],
    queryFn: () => listApplications(status),
    refetchInterval: 30000,
  });
}

export function useApplication(key: string | undefined) {
  return useQuery({
    queryKey: ["applications", key],
    queryFn: () => getApplication(key!),
    enabled: key !== undefined,
    refetchInterval: 30000,
  });
}

/** History is inherently backward-looking (a `since` window) --
 * refetched far less often than "current status" queries. */
export function useApplicationHistory(key: string | undefined, since?: string) {
  return useQuery({
    queryKey: ["applications", key, "history", since],
    queryFn: () => getApplicationHistory(key!, since),
    enabled: key !== undefined,
    refetchInterval: 30000,
  });
}
