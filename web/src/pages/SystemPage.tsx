import { useDoctor, useSystemStatus } from "@/hooks/useSystem";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { DoctorCheckBadge } from "@/components/status/DoctorCheckBadge";
import { RelativeTime } from "@/components/RelativeTime";
import { PageError } from "@/components/PageError";

const CHECK_LABELS: Record<string, string> = {
  configuration: "Configuration",
  database: "Database",
  docker_connection: "Docker connection",
  docker_read_access: "Docker read access",
  collector_heartbeat: "Collector heartbeat",
  remote_agents: "Remote agents",
  clock: "Clock",
};

export function SystemPage() {
  const doctor = useDoctor();
  const status = useSystemStatus();

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-lg font-semibold">Argus Self Health</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Argus's own operational health -- distinct from the health of the applications it monitors (see
          Applications for that).
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Doctor checks</CardTitle>
        </CardHeader>
        <CardContent>
          {doctor.isLoading ? (
            <Skeleton className="h-40 w-full" />
          ) : doctor.error ? (
            <PageError error={doctor.error} onRetry={() => void doctor.refetch()} />
          ) : (
            <div className="flex flex-col gap-1">
              {doctor.data!.checks.map((check) => (
                <div key={check.name} className="flex items-center justify-between border-b border-border py-2 last:border-0">
                  <div>
                    <p className="text-sm font-medium">{CHECK_LABELS[check.name] ?? check.name}</p>
                    {check.message && <p className="text-xs text-muted-foreground">{check.message}</p>}
                  </div>
                  <DoctorCheckBadge status={check.status} />
                </div>
              ))}
              <p className="mt-3 text-sm font-medium">
                {doctor.data!.operational ? "Argus is operational." : "Argus is not fully operational."}
              </p>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Collector state</CardTitle>
        </CardHeader>
        <CardContent>
          {status.isLoading ? (
            <Skeleton className="h-24 w-full" />
          ) : status.error ? (
            <PageError error={status.error} onRetry={() => void status.refetch()} />
          ) : (
            <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Field label="Status">{status.data!.collector.status}</Field>
              <Field label="Last tick">
                <RelativeTime iso={status.data!.collector.last_tick_at} />
              </Field>
              <Field label="Last success">
                <RelativeTime iso={status.data!.collector.last_success_at} />
              </Field>
              <Field label="Consecutive failures">{status.data!.collector.consecutive_failures}</Field>
              {status.data!.collector.last_error && (
                <div className="col-span-full">
                  <dt className="text-xs text-muted-foreground">Last error</dt>
                  <dd className="mt-0.5 font-mono text-xs text-destructive">{status.data!.collector.last_error}</dd>
                </div>
              )}
            </dl>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 text-sm font-medium">{children}</dd>
    </div>
  );
}
