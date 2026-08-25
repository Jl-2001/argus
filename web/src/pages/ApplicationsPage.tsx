import { useState } from "react";
import { Link } from "react-router-dom";
import { AppWindow } from "lucide-react";
import { useApplications } from "@/hooks/useApplications";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { HealthBadge } from "@/components/status/HealthBadge";
import { RelativeTime } from "@/components/RelativeTime";
import { PageError } from "@/components/PageError";
import { EmptyState } from "@/components/EmptyState";
import { HEALTH_STATUSES } from "@/lib/status";

const FILTERS = ["ALL", ...HEALTH_STATUSES] as const;
type Filter = (typeof FILTERS)[number];

export function ApplicationsPage() {
  const [filter, setFilter] = useState<Filter>("ALL");
  const { data, isLoading, error, refetch } = useApplications(filter === "ALL" ? undefined : filter);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Applications</h1>
      </div>

      <Tabs value={filter} onValueChange={(value) => setFilter(value as Filter)}>
        <TabsList>
          {FILTERS.map((value) => (
            <TabsTrigger key={value} value={value}>
              {value === "ALL" ? "All" : value.charAt(0) + value.slice(1).toLowerCase()}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      {isLoading ? (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      ) : error ? (
        <PageError error={error} onRetry={() => void refetch()} />
      ) : !data || data.length === 0 ? (
        <EmptyState
          icon={AppWindow}
          message={filter === "ALL" ? "No applications discovered yet." : `No ${filter.toLowerCase()} applications.`}
        />
      ) : (
        <div className="hidden overflow-hidden rounded-lg border border-border sm:block">
          <table className="w-full text-sm">
            <thead className="border-b border-border bg-muted/50 text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-4 py-2 font-medium">Name</th>
                <th className="px-4 py-2 font-medium">Host</th>
                <th className="px-4 py-2 font-medium">Status</th>
                <th className="px-4 py-2 font-medium">Services</th>
                <th className="px-4 py-2 font-medium">Containers</th>
                <th className="px-4 py-2 font-medium">Last seen</th>
              </tr>
            </thead>
            <tbody>
              {data.map((app) => (
                <tr key={app.key} className="border-b border-border last:border-0 hover:bg-accent/50">
                  <td className="px-4 py-2.5">
                    <Link to={`/applications/${app.key}`} className="font-medium hover:underline">
                      {app.name}
                    </Link>
                  </td>
                  <td className="px-4 py-2.5 text-muted-foreground">{app.host_name}</td>
                  <td className="px-4 py-2.5">
                    <HealthBadge status={app.status} />
                  </td>
                  <td className="px-4 py-2.5 text-muted-foreground">{app.services}</td>
                  <td className="px-4 py-2.5 text-muted-foreground">{app.containers}</td>
                  <td className="px-4 py-2.5 text-muted-foreground">
                    <RelativeTime iso={app.last_seen_at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Mobile: cards instead of a table (see the milestone's own
          Responsive Design section) -- shown only below sm via
          Tailwind, table above is hidden below sm via its own wrapper. */}
      <MobileApplicationCards data={data} />
    </div>
  );
}

function MobileApplicationCards({ data }: { data: ReturnType<typeof useApplications>["data"] }) {
  if (!data || data.length === 0) return null;
  return (
    <div className="flex flex-col gap-2 sm:hidden">
      {data.map((app) => (
        <Link key={app.key} to={`/applications/${app.key}`}>
          <Card>
            <CardContent className="flex items-center justify-between pt-4">
              <div>
                <p className="font-medium">{app.name}</p>
                <p className="text-xs text-muted-foreground">
                  {app.host_name} · {app.services} services · {app.containers} containers
                </p>
              </div>
              <HealthBadge status={app.status} />
            </CardContent>
          </Card>
        </Link>
      ))}
    </div>
  );
}
