import { cn } from "@/lib/utils";

/** Every page's loading state is built from this, not a "Loading…"
 * string -- keeps the layout stable (no page jump) while data streams
 * in. See the milestone's own "Loading States" section. */
export function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("animate-pulse rounded-md bg-muted", className)} {...props} />;
}
