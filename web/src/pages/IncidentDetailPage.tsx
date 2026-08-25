import { useParams, Link } from "react-router-dom";
import { useIncident } from "@/hooks/useIncidents";
import { useIncidentBundle, useIncidentExplanations } from "@/hooks/useEvidence";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { HealthBadge } from "@/components/status/HealthBadge";
import { IncidentBadge } from "@/components/status/IncidentBadge";
import { RelativeTime } from "@/components/RelativeTime";
import { PageError } from "@/components/PageError";
import { CitationProvider } from "@/components/evidence/CitationContext";
import { TimelineList } from "@/components/evidence/TimelineList";
import { EvidenceList } from "@/components/evidence/EvidenceList";
import { EvidenceBundleMeta } from "@/components/evidence/EvidenceBundleMeta";
import { AIExplanationPanel } from "@/components/evidence/AIExplanationPanel";
import { Sparkles } from "lucide-react";

export function IncidentDetailPage() {
  const params = useParams<{ id: string }>();
  const incidentId = params.id ? Number(params.id) : undefined;

  const incident = useIncident(incidentId);
  const bundle = useIncidentBundle(incidentId);
  const explanations = useIncidentExplanations(incidentId);

  if (incident.isLoading) return <DetailSkeleton />;
  if (incident.error) return <PageError error={incident.error} onRetry={() => void incident.refetch()} />;
  if (!incident.data) return null;

  const data = incident.data;

  return (
    <CitationProvider>
      <div className="flex flex-col gap-6">
        <div>
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-lg font-semibold">Incident #{data.id}</h1>
            <Link to={`/applications/${data.application_key}`} className="text-sm text-muted-foreground hover:underline">
              {data.application_name}
            </Link>
            <IncidentBadge status={data.status} />
            <HealthBadge status={data.worst_status} />
          </div>
          <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
            <Field label="Opened">
              <RelativeTime iso={data.opened_at} />
            </Field>
            <Field label="Resolved">{data.closed_at ? <RelativeTime iso={data.closed_at} /> : "—"}</Field>
            <Field label="Opening status">
              <HealthBadge status={data.opening_status} />
            </Field>
            <Field label="Worst status">
              <HealthBadge status={data.worst_status} />
            </Field>
          </dl>
        </div>

        <Section title="Timeline">
          {bundle.isLoading ? (
            <Skeleton className="h-40 w-full" />
          ) : bundle.error ? (
            <PageError error={bundle.error} onRetry={() => void bundle.refetch()} />
          ) : (
            <TimelineList entries={bundle.data!.timeline} />
          )}
        </Section>

        <Section title="Evidence">
          {bundle.isLoading ? (
            <Skeleton className="h-40 w-full" />
          ) : bundle.error ? (
            <PageError error={bundle.error} />
          ) : (
            <EvidenceList signals={bundle.data!.signals} />
          )}
        </Section>

        <Section title="AI Analysis">
          {explanations.isLoading ? (
            <Skeleton className="h-40 w-full" />
          ) : explanations.error ? (
            <PageError error={explanations.error} onRetry={() => void explanations.refetch()} />
          ) : (
            <>
              <AIExplanationPanel explanations={explanations.data!.explanations} />
              {explanations.data!.explanations.length === 0 && data.has_cached_explanation && (
                // has_cached_explanation came from the incident-detail
                // endpoint (a separate, possibly-stale poll from the
                // explanations list) -- shown only as a hint if the two
                // ever briefly disagree, never a substitute for the
                // real list above.
                <p className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
                  <Sparkles className="size-3.5" /> AI analysis available
                </p>
              )}
            </>
          )}
        </Section>

        <div>
          {bundle.isLoading ? (
            <Skeleton className="h-24 w-full" />
          ) : bundle.error ? null : (
            <EvidenceBundleMeta bundle={bundle.data!} />
          )}
        </div>
      </div>
    </CitationProvider>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-0.5">{children}</dd>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

function DetailSkeleton() {
  return (
    <div className="flex flex-col gap-6">
      <Skeleton className="h-16 w-full" />
      <Skeleton className="h-48 w-full" />
      <Skeleton className="h-48 w-full" />
      <Skeleton className="h-48 w-full" />
    </div>
  );
}
