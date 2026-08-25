import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { citationElementId } from "@/lib/citation";

interface CitationContextValue {
  activeReference: string | null;
  activate: (reference: string) => void;
}

const CitationContext = createContext<CitationContextValue | null>(null);

/**
 * Makes Argus's evidence citations ("[log_signal:42]") clickable
 * across the whole Incident Detail page: an `<CitationLink>` anywhere
 * inside this provider (an AI explanation's claim, say) can activate a
 * reference, and any `<Citable>` wrapping a Timeline/Evidence row for
 * that same reference scrolls into view and highlights -- this is what
 * makes Argus's grounding visually demonstrable (see the milestone's
 * own "Evidence Citation Interaction" section). One provider per
 * incident page; never global, since references are only meaningful
 * within one incident's own bundle.
 */
export function CitationProvider({ children }: { children: ReactNode }) {
  const [activeReference, setActiveReference] = useState<string | null>(null);

  const activate = useCallback((reference: string) => {
    setActiveReference(reference);
    // Prefer the more detailed Evidence-section row when one exists
    // for this reference (log_signal citations only); the Timeline
    // row always exists for every reference type, so it's the
    // fallback.
    const target =
      document.getElementById(citationElementId(reference, "evidence")) ??
      document.getElementById(citationElementId(reference, "timeline"));
    target?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, []);

  const value = useMemo(() => ({ activeReference, activate }), [activeReference, activate]);
  return <CitationContext.Provider value={value}>{children}</CitationContext.Provider>;
}

export function useCitation(): CitationContextValue {
  const ctx = useContext(CitationContext);
  if (!ctx) throw new Error("useCitation must be used within a CitationProvider");
  return ctx;
}
