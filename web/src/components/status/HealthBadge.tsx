import { healthStyle } from "@/lib/status";
import { cn } from "@/lib/utils";
import { StatusDot } from "./StatusDot";

/** Renders any `HealthStatus` string (HEALTHY/DEGRADED/UNHEALTHY/
 * STOPPED/RESTARTING/UNKNOWN) -- the one component every page uses for
 * this, so status styling is never hardcoded per-page (see
 * `src/lib/status.ts`'s own docstring). */
export function HealthBadge({ status, className }: { status: string; className?: string }) {
  const style = healthStyle(status);
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium",
        style.badgeClassName,
        className,
      )}
    >
      <StatusDot className={style.dotClassName} />
      {style.label}
    </span>
  );
}
