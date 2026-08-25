import { useState } from "react";
import { Link } from "react-router-dom";
import { Activity } from "lucide-react";
import { useIncidents } from "@/hooks/useIncidents";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Skeleton } from "@/components/ui/skeleton";
import { IncidentBadge } from "@/components/status/IncidentBadge";
import { HealthBadge } from "@/components/status/HealthBadge";
import { RelativeTime } from "@/components/RelativeTime";
import { PageError } from "@/components/PageError";
import { EmptyState } from "@/components/EmptyState";

type Filter = "open" | "all";

export function IncidentsPage() {
  const [filter, setFilter] = useState<Filter>("open");
  const { data, isLoading, error, refetch } = useIncidents(filter);

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-lg font-semibold">Incidents</h1>

      <Tabs value={filter} onValueChange={(value) => setFilter(value as Filter)}>
        <TabsList>
          <TabsTrigger value="open">Open</TabsTrigger>
          <TabsTrigger value="all">All</TabsTrigger>
        </TabsList>
      </Tabs>

      {isLoading ? (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-14 w-full" />
          <Skeleton className="h-14 w-full" />
        </div>
      ) : error ? (
        <PageError error={error} onRetry={() => void refetch()} />
      ) : !data || data.incidents.length === 0 ? (
        <EmptyState icon={Activity} message={filter === "open" ? "No open incidents." : "No incidents recorded."} />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full min-w-[640px] text-sm">
            <thead className="border-b border-border bg-muted/50 text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-4 py-2 font-medium">ID</th>
                <th className="px-4 py-2 font-medium">Application</th>
                <th className="px-4 py-2 font-medium">Status</th>
                <th className="px-4 py-2 font-medium">Opening</th>
                <th className="px-4 py-2 font-medium">Worst</th>
                <th className="px-4 py-2 font-medium">Opened</th>
                <th className="px-4 py-2 font-medium">Closed</th>
              </tr>
            </thead>
            <tbody>
              {data.incidents.map((incident) => (
                <tr key={incident.id} className="border-b border-border last:border-0 hover:bg-accent/50">
                  <td className="px-4 py-2.5">
                    <Link to={`/incidents/${incident.id}`} className="font-medium hover:underline">
                      #{incident.id}
                    </Link>
                  </td>
                  <td className="px-4 py-2.5">{incident.application}</td>
                  <td className="px-4 py-2.5">
                    <IncidentBadge status={incident.status} />
                  </td>
                  <td className="px-4 py-2.5">
                    <HealthBadge status={incident.opening_status} />
                  </td>
                  <td className="px-4 py-2.5">
                    <HealthBadge status={incident.worst_status} />
                  </td>
                  <td className="px-4 py-2.5 text-muted-foreground">
                    <RelativeTime iso={incident.opened_at} />
                  </td>
                  <td className="px-4 py-2.5 text-muted-foreground">
                    {incident.closed_at ? <RelativeTime iso={incident.closed_at} /> : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
