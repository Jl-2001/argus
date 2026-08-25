import { useState } from "react";
import { ChevronRight, Code2 } from "lucide-react";
import type { EvidenceBundleResponse } from "@/api/types";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { fullTimestamp } from "@/lib/format";
import { cn } from "@/lib/utils";

/** The "advanced/debugging" technical section the milestone asks for:
 * collapsed by default, and never dumping the full bundle JSON
 * un-asked-for -- "View JSON" is an explicit opt-in that then shows
 * exactly the already-bounded API payload this page already fetched
 * (nothing re-requested, nothing invented). */
export function EvidenceBundleMeta({ bundle }: { bundle: EvidenceBundleResponse }) {
  const [open, setOpen] = useState(false);
  const meta = bundle.metadata;

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger asChild>
        <button
          type="button"
          className="flex w-full items-center gap-1.5 text-left text-sm font-semibold text-muted-foreground hover:text-foreground"
        >
          <ChevronRight className={cn("size-4 shrink-0 transition-transform", open && "rotate-90")} aria-hidden="true" />
          Evidence Bundle
        </button>
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-3">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
          <MetaField label="Fingerprint">
            <span className="font-mono text-xs" title={meta.fingerprint}>
              {meta.fingerprint.slice(0, 12)}…
            </span>
          </MetaField>
          <MetaField label="Signals">{bundle.signals.length}</MetaField>
          <MetaField label="Transitions">{bundle.transitions.length}</MetaField>
          <MetaField label="Observations">{bundle.observations.length}</MetaField>
          <MetaField label="Truncated">{meta.truncated ? "Yes" : "No"}</MetaField>
          <MetaField label="Evidence subsystem">{meta.evidence_subsystem_status}</MetaField>
          <MetaField label="Window start" mono>
            {fullTimestamp(bundle.window.start)}
          </MetaField>
          <MetaField label="Window end" mono>
            {fullTimestamp(bundle.window.end)}
          </MetaField>
          <MetaField label="Assembler">{meta.assembler_version}</MetaField>
        </dl>

        <Dialog>
          <DialogTrigger asChild>
            <Button variant="outline" size="sm" className="mt-3">
              <Code2 className="size-3.5" /> View JSON
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Evidence bundle JSON</DialogTitle>
            </DialogHeader>
            <pre className="overflow-auto rounded-md bg-muted p-3 font-mono text-xs">
              {JSON.stringify(bundle, null, 2)}
            </pre>
          </DialogContent>
        </Dialog>
      </CollapsibleContent>
    </Collapsible>
  );
}

function MetaField({ label, mono, children }: { label: string; mono?: boolean; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={cn("font-medium", mono && "font-mono text-xs font-normal")}>{children}</dd>
    </div>
  );
}
