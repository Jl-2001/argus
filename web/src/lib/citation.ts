/** Turns an evidence reference string (e.g. `"log_signal:42"`,
 * `"health_transition:18"`, `"observation:7"`) into a valid HTML `id`,
 * scoped to which section is rendering it -- a given reference can
 * appear in both the Timeline and the Evidence sections at once (see
 * `src/components/evidence/CitationContext.tsx`), so each needs its
 * own element to scroll/highlight independently. */
export function citationElementId(reference: string, section: "timeline" | "evidence"): string {
  return `${section}-${reference.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
}
