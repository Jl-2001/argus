import type { ReactNode } from "react";
import { WifiOff } from "lucide-react";
import { useSystemStatus } from "@/hooks/useSystem";
import { ApiUnreachableError } from "@/api/client";
import { Button } from "@/components/ui/button";
import { ARGUS_API_URL } from "@/lib/env";

/**
 * Wraps the whole app. `GET /api/v1/system/status` is already polled
 * every 5s (see `useSystemStatus`) and doubles as this app's
 * connectivity heartbeat: if the *request itself* never reached the
 * API (`ApiUnreachableError`, not e.g. a 503 the API returned on
 * purpose), every page is replaced with a single, unambiguous "API
 * offline" screen rather than letting each page separately render an
 * empty/misleading "healthy" state from stale or absent data -- see
 * the milestone's own "API Offline State" section: a failed fetch must
 * never be interpreted as empty/healthy.
 */
export function ApiOfflineGate({ children }: { children: ReactNode }) {
  const { error, refetch, isRefetching } = useSystemStatus();

  if (error instanceof ApiUnreachableError) {
    return (
      <div className="flex min-h-dvh flex-col items-center justify-center gap-4 bg-background p-6 text-center">
        <WifiOff className="size-10 text-destructive" aria-hidden="true" />
        <div>
          <h1 className="text-lg font-semibold">ARGUS API OFFLINE</h1>
          <p className="mt-1 max-w-sm text-sm text-muted-foreground">
            Unable to reach the Argus API at <code className="font-mono">{ARGUS_API_URL}</code>. The
            dashboard cannot show application, incident, or system data until it's back.
          </p>
        </div>
        <Button onClick={() => void refetch()} disabled={isRefetching}>
          {isRefetching ? "Retrying…" : "Retry"}
        </Button>
      </div>
    );
  }

  return <>{children}</>;
}
