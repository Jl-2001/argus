import { useQuery } from "@tanstack/react-query";
import { getHost, listHosts } from "@/api/hosts";

/** Milestone 16. Polled every 30s -- fallback only; realtime events
 * that touch application/incident state already invalidate `["system"]`
 * (which a host's own online/offline classification indirectly tracks
 * via `last_seen_at`), same cadence as `useApplications`/`useSystemStatus`. */
export function useHosts() {
  return useQuery({
    queryKey: ["hosts"],
    queryFn: () => listHosts(),
    refetchInterval: 30000,
  });
}

export function useHost(hostKey: string | undefined) {
  return useQuery({
    queryKey: ["hosts", hostKey],
    queryFn: () => getHost(hostKey!),
    enabled: hostKey !== undefined,
    refetchInterval: 30000,
  });
}
