import { QueryClient } from "@tanstack/react-query";

/**
 * One shared QueryClient -- server state lives here (TanStack Query),
 * never in a hand-rolled global store (see the milestone's own
 * "Frontend Technology" section: no Redux, no giant client store).
 * Per-query `refetchInterval`s (see `src/hooks/*`) are the polling
 * mechanism for v0.1; `staleTime` here is a light default so a query
 * without its own explicit interval still refetches on refocus rather
 * than going stale forever.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 2000,
      retry: 1,
      refetchOnWindowFocus: true,
    },
  },
});
