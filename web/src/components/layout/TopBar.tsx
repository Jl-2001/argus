import { Menu, Moon, Sun } from "lucide-react";
import { useSystemStatus } from "@/hooks/useSystem";
import { useTheme } from "@/hooks/useTheme";
import { useRealtimeConnectionState } from "@/realtime/RealtimeProvider";
import type { ConnectionState } from "@/realtime/types";
import { Button } from "@/components/ui/button";
import { StatusDot } from "@/components/status/StatusDot";
import { cn } from "@/lib/utils";

// Collector *classification* (NEVER_RUN/STALE/FAILING/HEALTHY) is a
// distinct concept from application HealthStatus -- see
// argus.cli.queries.CollectorStatusView's own docstring -- so it gets
// its own small label/color mapping here rather than overloading
// src/lib/status.ts's HealthStatus union with a value that isn't one.
const COLLECTOR_LABELS: Record<string, { label: string; dot: string }> = {
  HEALTHY: { label: "Monitoring", dot: "bg-status-healthy" },
  FAILING: { label: "Monitoring degraded", dot: "bg-status-degraded" },
  STALE: { label: "Monitoring stale", dot: "bg-status-unhealthy" },
  NEVER_RUN: { label: "Never started", dot: "bg-status-unknown" },
};

// The SSE connection's own health -- a *different* signal from
// collector health above (see the milestone's own "SSE Connection
// State" section: "Do not confuse SSE connection health with Argus
// collector health"). A "Live"/"Reconnecting" SSE badge says nothing
// about whether Argus itself is monitoring anything; it only says
// whether this browser tab is currently getting instant updates or
// falling back to its existing polling.
const REALTIME_LABELS: Record<ConnectionState, { label: string; dot: string }> = {
  connecting: { label: "Connecting…", dot: "bg-status-unknown" },
  live: { label: "Live", dot: "bg-status-healthy" },
  reconnecting: { label: "Reconnecting…", dot: "bg-status-degraded" },
  offline: { label: "Realtime disconnected — polling", dot: "bg-status-unknown" },
};

export function TopBar({ onOpenMenu }: { onOpenMenu: () => void }) {
  const { data } = useSystemStatus();
  const { theme, toggleTheme } = useTheme();
  const realtimeState = useRealtimeConnectionState();

  const collector = data ? COLLECTOR_LABELS[data.collector.status] ?? COLLECTOR_LABELS.NEVER_RUN : undefined;
  const realtime = REALTIME_LABELS[realtimeState];

  return (
    <header className="flex h-14 shrink-0 items-center justify-between border-b border-border bg-background px-4">
      <div className="flex items-center gap-3">
        <Button
          variant="ghost" size="icon" className="md:hidden" onClick={onOpenMenu} aria-label="Open navigation menu"
        >
          <Menu className="size-5" />
        </Button>
        <span className="text-sm font-bold tracking-widest">ARGUS</span>
      </div>

      <div className="flex items-center gap-3">
        <span className="hidden items-center gap-2 text-xs font-medium text-muted-foreground sm:flex" title="Realtime (SSE) connection">
          <StatusDot className={cn(realtime.dot)} />
          {realtime.label}
        </span>
        {collector && (
          <span className="flex items-center gap-2 text-xs font-medium text-muted-foreground" title="Argus collector">
            <StatusDot className={cn(collector.dot)} />
            {collector.label}
          </span>
        )}
        <Button
          variant="ghost" size="icon" onClick={toggleTheme}
          aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
        >
          {theme === "dark" ? <Sun className="size-4" /> : <Moon className="size-4" />}
        </Button>
      </div>
    </header>
  );
}
