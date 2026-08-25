import type { QueryClient } from "@tanstack/react-query";
import type { RealtimeEventType } from "./types";

/**
 * The one central event-type -> query-invalidation map (see the
 * milestone's own "Frontend Event Handling" section: "Centralize this
 * mapping. Do not put EventSource handlers inside individual pages.").
 *
 * Every invalidation targets a *prefix*, not a full query key --
 * `queryClient.invalidateQueries({queryKey: ["applications"]})` matches
 * every query whose key starts with `"applications"` (the list, every
 * `application(key)` detail, every `history` query), not just an exact
 * key. This is deliberately broad rather than surgically precise: it
 * means an event never needs to enumerate every query key it might
 * affect, and TanStack Query only actually refetches queries that are
 * currently mounted/active -- an invalidated-but-unmounted query just
 * refetches next time something observes it. See `emit_evidence_updated`'s
 * own docstring for why `evidence.updated` in particular can only ever
 * be this broad (the backend event itself carries no
 * application/incident id to target more precisely).
 *
 * SSE events are never authoritative here -- every branch below ends
 * in "go refetch the real GET endpoint," never a direct cache write
 * from the event's own payload (see the milestone's own "No Direct
 * State Mutation" section).
 */
export function invalidateForEvent(queryClient: QueryClient, type: RealtimeEventType): void {
  switch (type) {
    case "collector.tick":
      // Also the local host's own heartbeat (`CollectorLoop` advances
      // it on every successful tick -- Milestone 16) -- see `["hosts"]`
      // below.
      void queryClient.invalidateQueries({ queryKey: ["system", "status"] });
      void queryClient.invalidateQueries({ queryKey: ["hosts"] });
      return;

    case "application.status_changed":
    case "service.status_changed":
    case "container.status_changed":
      void queryClient.invalidateQueries({ queryKey: ["applications"] }); // list + detail + history
      void queryClient.invalidateQueries({ queryKey: ["system", "status"] });
      // A remote agent's ingest is what produces these events for its
      // own applications, and every valid ingest also advances that
      // host's own heartbeat -- so a host's ONLINE/STALE/OFFLINE
      // status (and its application list/count) may have changed too.
      void queryClient.invalidateQueries({ queryKey: ["hosts"] });
      return;

    case "incident.opened":
    case "incident.updated":
    case "incident.resolved":
      void queryClient.invalidateQueries({ queryKey: ["incidents"] }); // list + detail + evidence + bundle + explanations
      void queryClient.invalidateQueries({ queryKey: ["applications"] }); // open_incident on the affected application
      void queryClient.invalidateQueries({ queryKey: ["system", "status"] }); // open_incidents count
      return;

    case "evidence.updated":
      void queryClient.invalidateQueries({ queryKey: ["incidents"] }); // covers evidence/bundle for any mounted incident
      return;

    case "evidence.health_changed":
      void queryClient.invalidateQueries({ queryKey: ["system"] }); // status + doctor
      return;

    case "explanation.available":
      void queryClient.invalidateQueries({ queryKey: ["incidents"] }); // explanations list/latest + has_cached_explanation
      return;
  }
}

/** `stream.reset` (retention gap on reconnect) -- invalidate every major
 * query family at once, per the milestone's own "Reconnect" section. */
export function invalidateEverything(queryClient: QueryClient): void {
  void queryClient.invalidateQueries({ queryKey: ["system"] });
  void queryClient.invalidateQueries({ queryKey: ["applications"] });
  void queryClient.invalidateQueries({ queryKey: ["incidents"] });
  void queryClient.invalidateQueries({ queryKey: ["hosts"] });
}
