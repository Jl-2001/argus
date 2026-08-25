import { Suspense, lazy } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import { queryClient } from "@/lib/queryClient";
import { RealtimeProvider } from "@/realtime/RealtimeProvider";
import { TooltipProvider } from "@/components/ui/tooltip";
import { AppShell } from "@/components/layout/AppShell";
import { Skeleton } from "@/components/ui/skeleton";
import { OverviewPage } from "@/pages/OverviewPage";
import { ApplicationsPage } from "@/pages/ApplicationsPage";
import { IncidentsPage } from "@/pages/IncidentsPage";
import { SystemPage } from "@/pages/SystemPage";
import { HostsPage } from "@/pages/HostsPage";

// Code-split the two pages that pull in React Flow / Recharts -- the
// heaviest dependencies in this app -- rather than shipping them in
// the initial bundle every route pays for.
const ApplicationDetailPage = lazy(() =>
  import("@/pages/ApplicationDetailPage").then((m) => ({ default: m.ApplicationDetailPage })),
);
const IncidentDetailPage = lazy(() =>
  import("@/pages/IncidentDetailPage").then((m) => ({ default: m.IncidentDetailPage })),
);

function PageFallback() {
  return <Skeleton className="h-64 w-full" />;
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <RealtimeProvider>
        <TooltipProvider delayDuration={200}>
          <BrowserRouter>
            <Routes>
              <Route element={<AppShell />}>
                <Route path="/" element={<OverviewPage />} />
                <Route path="/applications" element={<ApplicationsPage />} />
                <Route
                  path="/applications/:key"
                  element={
                    <Suspense fallback={<PageFallback />}>
                      <ApplicationDetailPage />
                    </Suspense>
                  }
                />
                <Route path="/incidents" element={<IncidentsPage />} />
                <Route
                  path="/incidents/:id"
                  element={
                    <Suspense fallback={<PageFallback />}>
                      <IncidentDetailPage />
                    </Suspense>
                  }
                />
                <Route path="/system" element={<SystemPage />} />
                <Route path="/hosts" element={<HostsPage />} />
              </Route>
            </Routes>
          </BrowserRouter>
        </TooltipProvider>
      </RealtimeProvider>
    </QueryClientProvider>
  );
}
