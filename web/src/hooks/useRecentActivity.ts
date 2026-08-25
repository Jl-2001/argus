import { useQueries } from "@tanstack/react-query";
import { getApplicationHistory } from "@/api/applications";
import type { ApplicationSummaryResponse, TransitionResponse } from "@/api/types";

export interface RecentActivityEntry extends TransitionResponse {
  applicationKey: string;
}

/**
 * The overview page's cross-application "Recent Activity" feed. The
 * backend has no single "recent transitions across every application"
 * endpoint (history is scoped to one application, by design -- see
 * `argus.cli.queries.list_history`), so this composes it client-side
 * from each known application's own short (`since=2h`) history, run in
 * parallel via `useQueries`. Homelab-scale by design (Argus's own
 * stated scope): a handful of applications, each already-bounded
 * history call -- this is not an N+1 query against something large.
 * Results are merged, sorted newest-first, and the caller (Overview)
 * caps how many are actually displayed.
 */
export function useRecentActivity(applications: ApplicationSummaryResponse[] | undefined) {
  const keys = applications?.map((app) => app.key) ?? [];

  const queries = useQueries({
    queries: keys.map((key) => ({
      queryKey: ["applications", key, "history", "2h"],
      queryFn: () => getApplicationHistory(key, "2h"),
      refetchInterval: 15000,
    })),
  });

  const isLoading = queries.some((q) => q.isLoading);
  const entries: RecentActivityEntry[] = queries
    .flatMap((q, index) => (q.data?.transitions ?? []).map((t) => ({ ...t, applicationKey: keys[index]! })))
    .sort((a, b) => (b.occurred_at ?? "").localeCompare(a.occurred_at ?? ""));

  return { entries, isLoading };
}
