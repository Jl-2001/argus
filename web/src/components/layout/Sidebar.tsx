import { NavLink } from "react-router-dom";
import { Activity, AppWindow, LayoutDashboard, Server, ShieldCheck } from "lucide-react";
import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
  { to: "/applications", label: "Applications", icon: AppWindow, end: false },
  { to: "/incidents", label: "Incidents", icon: Activity, end: false },
  { to: "/hosts", label: "Hosts", icon: Server, end: false },
  { to: "/system", label: "System", icon: ShieldCheck, end: false },
] as const;

export function SidebarNav({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav aria-label="Primary" className="flex flex-col gap-1 p-3">
      {NAV_ITEMS.map(({ to, label, icon: Icon, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          onClick={onNavigate}
          className={({ isActive }) =>
            cn(
              "flex items-center gap-2.5 rounded-md px-3 py-2 text-sm font-medium transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
              isActive
                ? "bg-secondary text-secondary-foreground"
                : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
            )
          }
        >
          <Icon className="size-4" aria-hidden="true" />
          {label}
        </NavLink>
      ))}
    </nav>
  );
}
