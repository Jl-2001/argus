import type { BundleTimelineEntryResponse } from "@/api/types";
import { EmptyState } from "@/components/EmptyState";
import { fullTimestamp, shortTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Citable } from "./CitationLink";
import { Activity, FileWarning, History } from "lucide-react";

const ENTRY_ICON: Record<string, typeof Activity> = {
  health_transition: Activity,
  log_signal: FileWarning,
  observation: History,
};

/** The incident's unified timeline -- backed only by
 * `EvidenceBundleResponse.timeline`, itself built mechanically from
 * real signal/transition/observation timestamps (see the backend's
 * own `argus.evidence.bundle.TimelineEntry` docstring: `facts` is
 * always structurally derived, never invented prose). Already
 * chronological; this component never re-sorts it. */
export function TimelineList({ entries }: { entries: BundleTimelineEntryResponse[] }) {
  if (entries.length === 0) {
    return <EmptyState icon={History} message="No timeline entries in this window." />;
  }

  return (
    <ol className="flex flex-col gap-1">
      {entries.map((entry, index) => {
        const Icon = ENTRY_ICON[entry.entry_type] ?? Activity;
        return (
          <li key={`${entry.reference}-${index}`}>
            <Citable reference={entry.reference} section="timeline" className="flex items-start gap-3 px-2 py-1.5">
              <Icon className={cn("mt-0.5 size-3.5 shrink-0 text-muted-foreground")} aria-hidden="true" />
              <span
                className="w-20 shrink-0 font-mono text-xs text-muted-foreground"
                title={fullTimestamp(entry.timestamp)}
              >
                {shortTime(entry.timestamp)}
              </span>
              <span className="min-w-0 flex-1 text-sm">
                <span className="font-medium">{entry.entity}</span>{" "}
                <span className="text-muted-foreground">{entry.facts}</span>
              </span>
            </Citable>
          </li>
        );
      })}
    </ol>
  );
}
