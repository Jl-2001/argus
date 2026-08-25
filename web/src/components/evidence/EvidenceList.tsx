import type { BundleSignalResponse } from "@/api/types";
import { EvidenceSeverityBadge } from "@/components/status/EvidenceSeverityBadge";
import { RelativeTime } from "@/components/RelativeTime";
import { EmptyState } from "@/components/EmptyState";
import { Citable } from "./CitationLink";
import { FileSearch } from "lucide-react";

/** Renders bundle signals (not the separate `/evidence` endpoint) --
 * `BundleSignalResponse` is a strict superset of `EvidenceItemResponse`
 * that additionally carries `reference`, which is what makes each row
 * a valid citation target (see `CitationContext`). Using one source
 * for both the Evidence section and citation resolution means there
 * is never a second, slightly-different "evidence" representation on
 * this page. */
export function EvidenceList({ signals }: { signals: BundleSignalResponse[] }) {
  if (signals.length === 0) {
    return <EmptyState icon={FileSearch} message="No evidence recorded for this incident." />;
  }

  return (
    <ul className="flex flex-col gap-2">
      {signals.map((signal) => (
        <li key={signal.reference}>
          <Citable reference={signal.reference} section="evidence" className="border border-border p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <EvidenceSeverityBadge severity={signal.severity} />
                <span className="font-mono text-sm font-medium">{signal.category}</span>
              </div>
              <span className="text-xs text-muted-foreground">
                Count {signal.count} · <RelativeTime iso={signal.last_seen_at} />
              </span>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">Source {signal.source_label}</p>
            <p className="mt-2 rounded bg-muted px-2 py-1.5 font-mono text-xs text-muted-foreground">
              {signal.sample}
            </p>
          </Citable>
        </li>
      ))}
    </ul>
  );
}
