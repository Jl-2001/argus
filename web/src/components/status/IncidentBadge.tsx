import { incidentStatusStyle } from "@/lib/status";
import { cn } from "@/lib/utils";
import { StatusDot } from "./StatusDot";

/** Renders an incident's own lifecycle status ("open"/"resolved") --
 * deliberately distinct from `HealthBadge`, which renders a
 * `HealthStatus` value; an incident's `status` and its
 * `opening_status`/`worst_status` are different concepts (see the
 * backend's own `IncidentRecord` docstring) and must never share one
 * badge component. */
export function IncidentBadge({ status, className }: { status: string; className?: string }) {
  const style = incidentStatusStyle(status);
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
