import { Server } from "lucide-react";
import { useHosts } from "@/hooks/useHosts";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { HostStatusBadge } from "@/components/status/HostStatusBadge";
import { RelativeTime } from "@/components/RelativeTime";
import { PageError } from "@/components/PageError";
import { EmptyState } from "@/components/EmptyState";

/** Milestone 16 -- read-only list of every registered monitored host
 * (the local machine plus any remote `argus-agent`). No management
 * buttons here (registering a host is a deliberate, administrative
 * CLI-only action -- `argus agents add`, never exposed through the
 * dashboard -- see the milestone's own "No web UI for agent management
 * yet"). */
export function HostsPage() {
  const { data, isLoading, error, refetch } = useHosts();

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-lg font-semibold">Hosts</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Every machine Argus monitors -- this one, and any remote host running its own <code>argus-agent</code>.
        </p>
      </div>

      {isLoading ? (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      ) : error ? (
        <PageError error={error} onRetry={() => void refetch()} />
      ) : !data || data.length === 0 ? (
        <EmptyState icon={Server} message="No hosts registered yet." />
      ) : (
        <div className="hidden overflow-hidden rounded-lg border border-border sm:block">
          <table className="w-full text-sm">
            <thead className="border-b border-border bg-muted/50 text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-4 py-2 font-medium">Host</th>
                <th className="px-4 py-2 font-medium">Status</th>
                <th className="px-4 py-2 font-medium">Last seen</th>
                <th className="px-4 py-2 font-medium">Agent version</th>
                <th className="px-4 py-2 font-medium">Applications</th>
              </tr>
            </thead>
            <tbody>
              {data.map((host) => (
                <tr key={host.host_key} className="border-b border-border last:border-0">
                  <td className="px-4 py-2.5 font-medium">
                    {host.display_name}
                    {host.kind === "local" && (
                      <span className="ml-2 rounded-full border border-border px-1.5 py-0.5 text-[10px] font-normal text-muted-foreground">
                        this machine
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2.5">
                    <HostStatusBadge status={host.status} />
                  </td>
                  <td className="px-4 py-2.5 text-muted-foreground">
                    <RelativeTime iso={host.last_seen_at} />
                  </td>
                  <td className="px-4 py-2.5 text-muted-foreground">{host.agent_version ?? "—"}</td>
                  <td className="px-4 py-2.5 text-muted-foreground">{host.application_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <MobileHostCards data={data} />
    </div>
  );
}

function MobileHostCards({ data }: { data: ReturnType<typeof useHosts>["data"] }) {
  if (!data || data.length === 0) return null;
  return (
    <div className="flex flex-col gap-2 sm:hidden">
      {data.map((host) => (
        <Card key={host.host_key}>
          <CardContent className="flex items-center justify-between pt-4">
            <div>
              <p className="font-medium">{host.display_name}</p>
              <p className="text-xs text-muted-foreground">
                {host.application_count} application{host.application_count === 1 ? "" : "s"} ·{" "}
                <RelativeTime iso={host.last_seen_at} />
              </p>
            </div>
            <HostStatusBadge status={host.status} />
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
