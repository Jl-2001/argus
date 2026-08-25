import { severityStyle } from "@/lib/status";
import { cn } from "@/lib/utils";
import { StatusDot } from "./StatusDot";

export function EvidenceSeverityBadge({ severity, className }: { severity: string; className?: string }) {
  const style = severityStyle(severity);
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium uppercase tracking-wide",
        style.badgeClassName,
        className,
      )}
    >
      <StatusDot className={style.dotClassName} />
      {style.label}
    </span>
  );
}
