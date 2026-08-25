import { ARGUS_API_URL } from "@/lib/env";

/**
 * The one place this app constructs an `EventSource` -- native browser
 * API, no WebSocket/socket.io library (see the milestone's own "Why
 * SSE" section: there is no browser-to-server real-time control need
 * here). Uses the same `ARGUS_API_URL` every other request goes
 * through (`src/api/client.ts`), never a second, hardcoded backend
 * URL.
 */
export function createArgusEventSource(): EventSource {
  return new EventSource(`${ARGUS_API_URL}/api/v1/events`);
}
