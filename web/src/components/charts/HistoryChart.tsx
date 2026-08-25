import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { TooltipContentProps } from "recharts";
import type { TransitionResponse } from "@/api/types";
import { EmptyState } from "@/components/EmptyState";
import { fullTimestamp, shortTime } from "@/lib/format";
import { healthStyle, HEALTH_STATUSES, type HealthStatus } from "@/lib/status";
import { History } from "lucide-react";

// A step chart over a *categorical* value needs an explicit ordinal
// position for each status -- this ranking exists only to place
// statuses sensibly on one axis (best at top); it is not a claim that
// health forms a true numeric scale. Nothing here fabricates a
// continuous metric: every point is a real transition timestamp/status
// pair from the API, never interpolated or invented (see the
// milestone's own "History Chart" section).
const STATUS_RANK: Record<HealthStatus, number> = {
  HEALTHY: 5, DEGRADED: 4, RESTARTING: 3, UNHEALTHY: 2, STOPPED: 1, UNKNOWN: 0,
};

interface ChartPoint {
  occurred_at: string;
  status: string;
  rank: number;
}

/** Application-scope health history as a stepped line -- one point per
 * real transition, never a fabricated continuous metric. */
export function HistoryChart({ transitions }: { transitions: TransitionResponse[] }) {
  const points: ChartPoint[] = transitions
    .filter((t) => t.occurred_at !== null)
    .map((t) => ({
      occurred_at: t.occurred_at!,
      status: t.to_status,
      rank: STATUS_RANK[t.to_status as HealthStatus] ?? STATUS_RANK.UNKNOWN,
    }));

  if (points.length === 0) {
    return <EmptyState icon={History} message="No health transitions recorded in this window." />;
  }

  return (
    <div className="h-56 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={points} margin={{ top: 8, right: 12, left: 8, bottom: 0 }}>
          <XAxis
            dataKey="occurred_at"
            tickFormatter={(value: string) => shortTime(value)}
            tick={{ fontSize: 11 }}
            stroke="var(--color-border)"
          />
          <YAxis
            domain={[0, 5]}
            ticks={HEALTH_STATUSES.map((s) => STATUS_RANK[s])}
            tickFormatter={(value: number) => {
              const status = HEALTH_STATUSES.find((s) => STATUS_RANK[s] === value);
              return status ? healthStyle(status).label : "";
            }}
            tick={{ fontSize: 11 }}
            width={72}
            stroke="var(--color-border)"
          />
          <Tooltip content={HistoryTooltip} />
          <Line
            type="stepAfter"
            dataKey="rank"
            stroke="var(--color-primary)"
            strokeWidth={2}
            dot={{ r: 3 }}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function HistoryTooltip({ active, payload }: TooltipContentProps) {
  if (!active || !payload?.length) return null;
  const point = payload[0]!.payload as ChartPoint;
  return (
    <div className="rounded-md border border-border bg-card px-3 py-2 text-xs text-card-foreground shadow-md">
      <p className="font-medium">{healthStyle(point.status).label}</p>
      <p className="text-muted-foreground">{fullTimestamp(point.occurred_at)}</p>
    </div>
  );
}
