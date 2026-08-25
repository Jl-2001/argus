import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { createArgusEventSource } from "./eventSource";
import { invalidateEverything, invalidateForEvent } from "./invalidation";
import { REALTIME_EVENT_TYPES, type ConnectionState } from "./types";

/**
 * Opens exactly one `EventSource` for the whole app (see
 * `RealtimeProvider`, which calls this once and shares the resulting
 * state via context -- no page/component ever creates its own), wires
 * every known event type to `invalidateForEvent`, and reports
 * connection state for `TopBar` to render.
 *
 * A malformed or unrecognized event never crashes the UI: JSON parsing
 * is wrapped in try/catch (a parse failure is simply ignored), and an
 * event type the browser wasn't told to listen for is already ignored
 * by `EventSource` itself -- there is no generic catch-all handler to
 * accidentally mishandle it.
 */
export function useArgusEvents(): ConnectionState {
  const queryClient = useQueryClient();
  const [state, setState] = useState<ConnectionState>("connecting");

  useEffect(() => {
    const source = createArgusEventSource();

    source.onopen = () => setState("live");
    source.onerror = () => {
      // Native EventSource auto-reconnects on a dropped connection
      // (readyState goes back to CONNECTING) -- only a browser-decided
      // CLOSED means it has given up and this app is the one that must
      // decide what "offline" means for the realtime indicator (see
      // TopBar). GET polling keeps the dashboard itself working either
      // way -- see the milestone's own "API Offline Behavior" section.
      setState(source.readyState === EventSource.CLOSED ? "offline" : "reconnecting");
    };

    for (const type of REALTIME_EVENT_TYPES) {
      source.addEventListener(type, (event: MessageEvent) => {
        try {
          JSON.parse(event.data as string);
        } catch {
          return; // malformed payload -- ignore, never crash the UI
        }
        invalidateForEvent(queryClient, type);
      });
    }

    source.addEventListener("stream.reset", () => {
      invalidateEverything(queryClient);
    });

    return () => {
      source.close();
    };
  }, [queryClient]);

  return state;
}
