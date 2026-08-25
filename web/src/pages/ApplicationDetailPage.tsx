import { useParams, Link } from "react-router-dom";
import { useApplication, useApplicationHistory } from "@/hooks/useApplications";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { HealthBadge } from "@/components/status/HealthBadge";
import { RelativeTime } from "@/components/RelativeTime";
import { PageError } from "@/components/PageError";
import { ApplicationTopology } from "@/components/topology/ApplicationTopology";
import { HistoryChart } from "@/components/charts/HistoryChart";

export function ApplicationDetailPage() {
  const { key } = useParams<{ key: string }>();
  const detail = useApplication(key);
  const history = useApplicationHistory(key, "24h");

  if (detail.isLoading) return <DetailSkeleton />;
  if (detail.error) return <PageError error={detail.error} onRetry={() => void detail.refetch()} />;
  if (!detail.data) return null;

  const app = detail.data;

  return (
    <div className="flex flex-col gap-6">
      <div>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-lg font-semibold">{app.name}</h1>
          <HealthBadge status={app.status} />
        </div>
        <p className="mt-1 text-sm text-muted-foreground">
          Host: {app.host_name} · Last observed <RelativeTime iso={app.last_seen_at} />
        </p>
      </div>

      {app.open_incident && (
        <Card className="border-status-unhealthy/30">
          <CardContent className="flex flex-wrap items-center justify-between gap-3 pt-4">
            <div>
              <p className="text-sm font-medium">
                Open incident #{app.open_incident.id} · {app.open_incident.worst_status}
              </p>
              <p className="text-xs text-muted-foreground">
                Opened <RelativeTime iso={app.open_incident.opened_at} />
              </p>
            </div>
            <Link to={`/incidents/${app.open_incident.id}`} className="text-sm font-medium text-primary hover:underline">
              View Incident →
            </Link>
          </CardContent>
        </Card>
      )}

      <section>
        <h2 className="mb-3 text-sm font-semibold text-muted-foreground">Topology</h2>
        <ApplicationTopology detail={app} />
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold text-muted-foreground">Services</h2>
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full min-w-[720px] text-sm">
            <thead className="border-b border-border bg-muted/50 text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-4 py-2 font-medium">Service</th>
                <th className="px-4 py-2 font-medium">Status</th>
                <th className="px-4 py-2 font-medium">Container</th>
                <th className="px-4 py-2 font-medium">Docker state</th>
                <th className="px-4 py-2 font-medium">Docker health</th>
                <th className="px-4 py-2 font-medium">Restarts</th>
                <th className="px-4 py-2 font-medium">Ports</th>
              </tr>
            </thead>
            <tbody>
              {app.services.map((service) => (
                <tr key={service.name} className="border-b border-border last:border-0">
                  <td className="px-4 py-2.5 font-medium">{service.name}</td>
                  <td className="px-4 py-2.5">
                    <HealthBadge status={service.status} />
                  </td>
                  <td className="px-4 py-2.5 text-muted-foreground">{service.container?.name ?? "—"}</td>
                  <td className="px-4 py-2.5 text-muted-foreground">{service.container?.docker_state ?? "—"}</td>
                  <td className="px-4 py-2.5 text-muted-foreground">{service.container?.docker_health ?? "—"}</td>
                  <td className="px-4 py-2.5 text-muted-foreground">{service.container?.restart_count ?? "—"}</td>
                  <td className="px-4 py-2.5 text-muted-foreground">
                    {service.container && service.container.ports.length > 0
                      ? service.container.ports
                          .map((p) => `${p.container_port}/${p.protocol}${p.host_binding ? ` → ${p.host_binding}` : ""}`)
                          .join(", ")
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold text-muted-foreground">History (24h)</h2>
        <Card>
          <CardHeader>
            <CardTitle className="text-xs font-normal text-muted-foreground">Health transitions over time</CardTitle>
          </CardHeader>
          <CardContent>
            {history.isLoading ? (
              <Skeleton className="h-56 w-full" />
            ) : history.error ? (
              <PageError error={history.error} />
            ) : (
              <HistoryChart transitions={(history.data?.transitions ?? []).filter((t) => t.scope === "application")} />
            )}
          </CardContent>
        </Card>
      </section>
    </div>
  );
}

function DetailSkeleton() {
  return (
    <div className="flex flex-col gap-6">
      <Skeleton className="h-8 w-48" />
      <Skeleton className="h-80 w-full" />
      <Skeleton className="h-40 w-full" />
      <Skeleton className="h-64 w-full" />
    </div>
  );
}
