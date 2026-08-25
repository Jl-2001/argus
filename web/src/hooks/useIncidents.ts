import { useQuery } from "@tanstack/react-query";
import { getIncident, listIncidents } from "@/api/incidents";

/** Polled every 30s -- fallback only; `incident.opened`/`.updated`/
 * `.resolved` over SSE (`src/realtime/`) deliver the immediate update.
 * See `useSystemStatus`'s own docstring for the same reasoning. */
export function useIncidents(status?: "open" | "all") {
  return useQuery({
    queryKey: ["incidents", { status }],
    queryFn: () => listIncidents(status),
    refetchInterval: 30000,
  });
}

/** A single incident's own metadata (status/worst-state/counts) --
 * same fallback cadence as the list. */
export function useIncident(id: number | undefined) {
  return useQuery({
    queryKey: ["incidents", id],
    queryFn: () => getIncident(id!),
    enabled: id !== undefined,
    refetchInterval: 30000,
  });
}
