import { useCitation } from "./CitationContext";
import { citationElementId } from "@/lib/citation";
import { cn } from "@/lib/utils";

/** One clickable evidence reference, e.g. `[log_signal:42]`. Renders
 * as a small button (not a link -- it navigates nowhere, it highlights
 * something already on this page), styled like inline code so it
 * reads as a citation. Keyboard-operable by default (`<button>`). */
export function CitationLink({ reference }: { reference: string }) {
  const { activate, activeReference } = useCitation();
  const isActive = activeReference === reference;

  return (
    <button
      type="button"
      onClick={() => activate(reference)}
      className={cn(
        "rounded border px-1.5 py-0.5 font-mono text-xs transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1",
        isActive
          ? "border-ring bg-accent text-accent-foreground"
          : "border-border bg-muted text-muted-foreground hover:bg-accent hover:text-accent-foreground",
      )}
    >
      [{reference}]
    </button>
  );
}

/** Wraps a Timeline/Evidence row so it can be scrolled-to and
 * highlighted when its `reference` is cited elsewhere on the page. */
export function Citable({
  reference, section, className, children,
}: {
  reference: string;
  section: "timeline" | "evidence";
  className?: string;
  children: React.ReactNode;
}) {
  const { activeReference } = useCitation();
  const isActive = activeReference === reference;

  return (
    <div
      id={citationElementId(reference, section)}
      className={cn(
        "rounded-md transition-colors",
        isActive && "bg-accent ring-2 ring-ring",
        className,
      )}
    >
      {children}
    </div>
  );
}
