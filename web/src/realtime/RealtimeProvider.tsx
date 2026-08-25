import { createContext, useContext, type ReactNode } from "react";
import { useArgusEvents } from "./useArgusEvents";
import type { ConnectionState } from "./types";

const RealtimeContext = createContext<ConnectionState>("connecting");

/**
 * Mounted once, at the top of the app (see `App.tsx`) -- the one place
 * `useArgusEvents` (and so the one `EventSource` connection) is ever
 * created. Every consumer (currently just `TopBar`) reads the shared
 * connection state via `useRealtimeConnectionState` instead of opening
 * its own connection (see the milestone's own "Performance" section:
 * "There should be ONE connection per dashboard app/session").
 */
export function RealtimeProvider({ children }: { children: ReactNode }) {
  const state = useArgusEvents();
  return <RealtimeContext.Provider value={state}>{children}</RealtimeContext.Provider>;
}

export function useRealtimeConnectionState(): ConnectionState {
  return useContext(RealtimeContext);
}
