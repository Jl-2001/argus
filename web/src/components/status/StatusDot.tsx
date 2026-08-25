import { cn } from "@/lib/utils";

/** A small colored dot -- always paired with visible text by its
 * caller (see HealthBadge etc.), never used alone as the only signal
 * of status (see the milestone's own Accessibility section: "Do not
 * rely solely on color/dots"). `aria-hidden` because the badge's own
 * text already carries the meaning for screen readers. */
export function StatusDot({ className }: { className: string }) {
  return <span aria-hidden="true" className={cn("inline-block size-2 rounded-full", className)} />;
}
