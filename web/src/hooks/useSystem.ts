import { useQuery } from "@tanstack/react-query";
import { getDoctor, getSystemStatus } from "@/api/system";

/** Polled every 30s -- SSE (`collector.tick` -> `RealtimeProvider`,
 * see `src/realtime/`) now delivers the immediate update; this
 * interval is the fallback that keeps the page eventually correct even
 * if the SSE connection is down (see the milestone's own "Keep Polling
 * as Fallback" section -- SSE is an acceleration mechanism, never a
 * single point of failure). */
export function useSystemStatus() {
  return useQuery({
    queryKey: ["system", "status"],
    queryFn: getSystemStatus,
    refetchInterval: 30000,
  });
}

/** Doctor performs live Docker diagnostics on the backend -- polled
 * much less aggressively (30s) since it's a "is Argus itself healthy"
 * check a human is reading, not a live incident feed. Also nudged by
 * `evidence.health_changed` over SSE. */
export function useDoctor() {
  return useQuery({
    queryKey: ["system", "doctor"],
    queryFn: getDoctor,
    refetchInterval: 30000,
  });
}
