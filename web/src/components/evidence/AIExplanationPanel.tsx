import { useState } from "react";
import type { ExplanationResponse } from "@/api/types";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Badge } from "@/components/ui/badge";
import { CitationLink } from "./CitationLink";
import { RelativeTime } from "@/components/RelativeTime";
import { EmptyState } from "@/components/EmptyState";
import { Sparkles } from "lucide-react";
import { cn } from "@/lib/utils";

const CONFIDENCE_STYLE: Record<string, string> = {
  low: "bg-status-unhealthy/15 text-status-unhealthy border-status-unhealthy/30",
  medium: "bg-status-degraded/15 text-status-degraded border-status-degraded/30",
  high: "bg-status-healthy/15 text-status-healthy border-status-healthy/30",
};

const PROVIDER_LABEL: Record<string, string> = { anthropic: "Anthropic", gemini: "Gemini" };

/**
 * Persisted, already-validated explanations only -- this component
 * never triggers generation (there is no such button anywhere in this
 * app; see Milestone 14's own scope). If more than one provider has a
 * cached explanation for this incident, they're switchable via tabs,
 * making the multi-provider architecture visible rather than silently
 * picking one.
 */
export function AIExplanationPanel({ explanations }: { explanations: ExplanationResponse[] }) {
  const [selectedProvider, setSelectedProvider] = useState<string | undefined>(explanations[0]?.provider);

  if (explanations.length === 0) {
    return (
      <EmptyState icon={Sparkles} message="No AI analysis has been generated for this incident." />
    );
  }

  // One tab per provider, most-recent explanation from that provider
  // shown per tab (an audit trail can hold more than one per provider
  // over time -- see the backend's own `list_explanations_for_incident`
  // docstring).
  const byProvider = new Map<string, ExplanationResponse>();
  for (const explanation of explanations) byProvider.set(explanation.provider, explanation);
  const providers = [...byProvider.keys()];
  const active = byProvider.get(selectedProvider ?? providers[0]!) ?? explanations[0]!;

  return (
    <div className="flex flex-col gap-4">
      {providers.length > 1 && (
        <Tabs value={active.provider} onValueChange={setSelectedProvider}>
          <TabsList>
            {providers.map((provider) => (
              <TabsTrigger key={provider} value={provider}>
                {PROVIDER_LABEL[provider] ?? provider}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
      )}

      <ExplanationDetail explanation={active} />
    </div>
  );
}

function ExplanationDetail({ explanation }: { explanation: ExplanationResponse }) {
  const body = explanation.explanation;
  const confidenceClass = CONFIDENCE_STYLE[body.confidence] ?? "bg-muted text-muted-foreground border-border";

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span>
          Provider <span className="font-medium text-foreground">{PROVIDER_LABEL[explanation.provider] ?? explanation.provider}</span>
        </span>
        <span>
          Model <span className="font-medium text-foreground">{explanation.model}</span>
        </span>
        <span>
          Generated <RelativeTime iso={explanation.created_at} className="font-medium text-foreground" />
        </span>
        {explanation.usage && (
          <span>
            Tokens{" "}
            <span className="font-medium text-foreground">
              {explanation.usage.input_tokens ?? "?"} in / {explanation.usage.output_tokens ?? "?"} out
            </span>
          </span>
        )}
        <span className="font-mono" title={explanation.bundle_fingerprint}>
          fp:{explanation.bundle_fingerprint.slice(0, 8)}
        </span>
      </div>

      <div>
        <span className={cn("inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium uppercase", confidenceClass)}>
          {body.confidence} confidence
        </span>
      </div>

      <p className="text-sm">{body.summary}</p>

      {body.root_cause_claim && (
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Probable root cause</h4>
          <p className="mt-1 text-sm">
            {body.root_cause_claim.text}{" "}
            {body.root_cause_claim.evidence_references.map((ref) => (
              <CitationLink key={ref} reference={ref} />
            ))}
          </p>
        </div>
      )}

      {body.supporting_claims.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Supporting evidence</h4>
          <ul className="mt-1 flex flex-col gap-1.5">
            {body.supporting_claims.map((claim, index) => (
              <li key={index} className="text-sm">
                {claim.text}{" "}
                {claim.evidence_references.map((ref) => (
                  <CitationLink key={ref} reference={ref} />
                ))}
              </li>
            ))}
          </ul>
        </div>
      )}

      {body.recommendation && (
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Recommended next step</h4>
          <p className="mt-1 text-sm">
            {body.recommendation.explanation ?? body.recommendation.category.replaceAll("_", " ")}
            {body.recommendation.explanation && (
              <Badge variant="outline" className="ml-2">
                {body.recommendation.category.replaceAll("_", " ")}
              </Badge>
            )}
          </p>
        </div>
      )}

      {body.caveats.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Caveats</h4>
          <ul className="mt-1 list-inside list-disc text-sm text-muted-foreground">
            {body.caveats.map((caveat, index) => (
              <li key={index}>{caveat}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
