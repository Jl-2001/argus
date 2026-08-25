import { useState } from "react";
import { Outlet } from "react-router-dom";
import { X } from "lucide-react";
import { TopBar } from "./TopBar";
import { SidebarNav } from "./Sidebar";
import { ApiOfflineGate } from "./ApiOfflineGate";
import { Button } from "@/components/ui/button";

/**
 * The persistent app layout: a top bar, a sidebar (fixed on desktop,
 * an overlay drawer below the `md` breakpoint -- see the milestone's
 * own Responsive Design section), and the routed page content.
 * `ApiOfflineGate` wraps the whole thing so an unreachable API replaces
 * every page with one unambiguous offline screen instead of each page
 * separately guessing.
 */
export function AppShell() {
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  return (
    <ApiOfflineGate>
      <div className="flex h-dvh flex-col">
        <TopBar onOpenMenu={() => setMobileNavOpen(true)} />

        <div className="flex min-h-0 flex-1">
          <aside className="hidden w-56 shrink-0 border-r border-border md:block">
            <SidebarNav />
          </aside>

          {mobileNavOpen && (
            <div className="fixed inset-0 z-40 md:hidden">
              <button
                type="button"
                aria-label="Close navigation menu"
                className="absolute inset-0 bg-black/50"
                onClick={() => setMobileNavOpen(false)}
              />
              <div className="relative z-50 flex h-full w-64 flex-col bg-background shadow-lg">
                <div className="flex h-14 items-center justify-between border-b border-border px-4">
                  <span className="text-sm font-bold tracking-widest">ARGUS</span>
                  <Button variant="ghost" size="icon" onClick={() => setMobileNavOpen(false)} aria-label="Close navigation menu">
                    <X className="size-5" />
                  </Button>
                </div>
                <SidebarNav onNavigate={() => setMobileNavOpen(false)} />
              </div>
            </div>
          )}

          <main className="min-w-0 flex-1 overflow-y-auto">
            <div className="mx-auto max-w-6xl p-4 md:p-6">
              <Outlet />
            </div>
          </main>
        </div>
      </div>
    </ApiOfflineGate>
  );
}
