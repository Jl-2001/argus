import type { LucideIcon } from "lucide-react";

/** A legitimate "nothing here" state -- distinct from `PageError`.
 * "No applications discovered yet" is not an error; it's an honest
 * fact about a fresh Argus install. See the milestone's own Empty
 * States section. */
export function EmptyState({ icon: Icon, message }: { icon: LucideIcon; message: string }) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-lg border border-dashed border-border p-8 text-center text-muted-foreground">
      <Icon className="size-8" aria-hidden="true" />
      <p className="text-sm">{message}</p>
    </div>
  );
}
