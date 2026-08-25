import { Link } from "react-router-dom";
import { AlertCircle, AppWindow, CheckCircle2, Clock } from "lucide-react";
import { useSystemStatus } from "@/hooks/useSystem";
import { useIncidents } from "@/hooks/useIncidents";
import { useRecentActivity } from "@/hooks/useRecentActivity";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { HealthBadge } from "@/components/status/HealthBadge";
import { RelativeTime } from "@/components/RelativeTime";
import { PageError } from "@/components/PageError";
import { EmptyState } from "@/components/EmptyState";
import { shortTime } from "@/lib/format";

const RECENT_ACTIVITY_LIMIT = 8;

export function OverviewPage() {
  const status = useSystemStatus();
  const openIncidents = useIncidents("open");
  const recentActivity = useRecentActivity(status.data?.applications);

  if (status.isLoading) return <OverviewSkeleton />;
  if (status.error) return <PageError error={status.error} onRetry={() => void status.refetch()} />;
  if (!status.data) return null;

  return (
    <div className="flex flex-col gap-6">
      <SystemSummary
        collectorStatus={status.data.collector.status}
        lastTickAt={status.data.collector.last_tick_at}
        lastSuccessAt={status.data.collector.last_success_at}
        openIncidents={status.data.open_incidents}
        applicationCount={status.data.applications.length}
      />

      <section>
        <h2 className="mb-3 text-sm font-semibold text-muted-foreground">Applications</h2>
        {status.data.applications.length === 0 ? (
          <EmptyState icon={AppWindow} message="No applications discovered yet." />
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {status.data.applications.map((app) => (
              <Link key={app.key} to={`/applications/${app.key}`}>
                <Card className="transition-colors hover:border-ring">
                  <CardHeader>
                    <div className="flex items-center justify-between">
                      <CardTitle>{app.name}</CardTitle>
                      <HealthBadge status={app.status} />
                    </div>
                  </CardHeader>
                  <CardContent className="text-xs text-muted-foreground">
                    {app.services} {app.services === 1 ? "service" : "services"}
                  </CardContent>
                </Card>
              </Link>
            ))}
          </div>
        )}
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold text-muted-foreground">Active Incidents</h2>
        {openIncidents.isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : openIncidents.error ? (
          <PageError error={openIncidents.error} />
        ) : openIncidents.data && openIncidents.data.incidents.length > 0 ? (
          <div className="flex flex-col gap-2">
            {openIncidents.data.incidents.map((incident) => (
              <Card key={incident.id} className="border-status-unhealthy/30">
                <CardContent className="flex flex-wrap items-center justify-between gap-3 pt-4">
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="font-semibold">#{incident.id}</span>
                      <span className="text-sm text-muted-foreground">{incident.application}</span>
                      <HealthBadge status={incident.worst_status} />
                    </div>
                    <p className="mt-1 text-xs text-muted-foreground">
                      Opened <RelativeTime iso={incident.opened_at} /> · Worst state {incident.worst_status}
                    </p>
                  </div>
                  <Button asChild size="sm" variant="outline">
                    <Link to={`/incidents/${incident.id}`}>View Incident</Link>
                  </Button>
                </CardContent>
              </Card>
            ))}
          </div>
        ) : (
          <EmptyState icon={CheckCircle2} message="No open incidents." />
        )}
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold text-muted-foreground">Recent Activity</h2>
        {recentActivity.isLoading && recentActivity.entries.length === 0 ? (
          <Skeleton className="h-32 w-full" />
        ) : recentActivity.entries.length === 0 ? (
          <EmptyState icon={Clock} message="No recent transitions." />
        ) : (
          <Card>
            <CardContent className="pt-4">
              <ol className="flex flex-col gap-1.5">
                {recentActivity.entries.slice(0, RECENT_ACTIVITY_LIMIT).map((entry, index) => (
                  <li key={`${entry.applicationKey}-${entry.occurred_at}-${index}`} className="flex items-center gap-3 text-sm">
                    <span className="w-20 shrink-0 font-mono text-xs text-muted-foreground">
                      {shortTime(entry.occurred_at)}
                    </span>
                    <span className="min-w-0 flex-1 truncate">
                      <span className="font-medium">{entry.label}</span>{" "}
                      <span className="text-muted-foreground">
                        {entry.from_status ?? "NULL"} → {entry.to_status}
                      </span>
                    </span>
                  </li>
                ))}
              </ol>
            </CardContent>
          </Card>
        )}
      </section>
    </div>
  );
}

function SystemSummary({
  collectorStatus, lastTickAt, lastSuccessAt, openIncidents, applicationCount,
}: {
  collectorStatus: string;
  lastTickAt: string | null;
  lastSuccessAt: string | null;
  openIncidents: number;
  applicationCount: number;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          ARGUS
          {openIncidents === 0 ? (
            <span className="inline-flex items-center gap-1 text-xs font-normal text-status-healthy">
              <CheckCircle2 className="size-3.5" /> Monitoring healthy
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 text-xs font-normal text-status-unhealthy">
              <AlertCircle className="size-3.5" /> {openIncidents} active {openIncidents === 1 ? "incident" : "incidents"}
            </span>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-4 sm:grid-cols-5">
        <SummaryStat label="Collector">
          <HealthBadge status={collectorStatus === "HEALTHY" ? "HEALTHY" : collectorStatus === "NEVER_RUN" ? "UNKNOWN" : "UNHEALTHY"} />
        </SummaryStat>
        <SummaryStat label="Last tick">
          <RelativeTime iso={lastTickAt} className="text-sm font-medium" />
        </SummaryStat>
        <SummaryStat label="Last successful tick">
          <RelativeTime iso={lastSuccessAt} className="text-sm font-medium" />
        </SummaryStat>
        <SummaryStat label="Open incidents">
          <span className="text-sm font-medium">{openIncidents}</span>
        </SummaryStat>
        <SummaryStat label="Applications">
          <span className="text-sm font-medium">{applicationCount}</span>
        </SummaryStat>
      </CardContent>
    </Card>
  );
}

function SummaryStat({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="text-xs text-muted-foreground">{label}</p>
      <div className="mt-1">{children}</div>
    </div>
  );
}

function OverviewSkeleton() {
  return (
    <div className="flex flex-col gap-6">
      <Skeleton className="h-32 w-full" />
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
      <Skeleton className="h-24 w-full" />
    </div>
  );
}
