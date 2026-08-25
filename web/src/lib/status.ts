/**
 * The single source of truth mapping every status-shaped string the
 * API returns to a color token (from `src/index.css`'s `@theme`) and a
 * human label. Every status badge component
 * (`src/components/status/*`) reads through here -- nothing hardcodes
 * "green" or "red" anywhere else, and no frontend-only status value is
 * ever invented: the keys below are exactly the enums
 * `argus.domain.models` / `argus.doctor.checks` / evidence severity
 * already define.
 */

export type HealthStatus = "HEALTHY" | "DEGRADED" | "UNHEALTHY" | "STOPPED" | "RESTARTING" | "UNKNOWN";

export const HEALTH_STATUSES: readonly HealthStatus[] = [
  "HEALTHY", "DEGRADED", "UNHEALTHY", "STOPPED", "RESTARTING", "UNKNOWN",
];

interface StatusStyle {
  label: string;
  colorVar: string; // a `--color-status-*` / `--color-severity-*` / `--color-*` token name
  dotClassName: string; // Tailwind class using that token, for the small status dot
  badgeClassName: string; // background + text classes for a pill badge
}

const HEALTH_STYLES: Record<HealthStatus, StatusStyle> = {
  HEALTHY: {
    label: "Healthy", colorVar: "--color-status-healthy",
    dotClassName: "bg-status-healthy",
    badgeClassName: "bg-status-healthy/15 text-status-healthy border-status-healthy/30",
  },
  DEGRADED: {
    label: "Degraded", colorVar: "--color-status-degraded",
    dotClassName: "bg-status-degraded",
    badgeClassName: "bg-status-degraded/15 text-status-degraded border-status-degraded/30",
  },
  UNHEALTHY: {
    label: "Unhealthy", colorVar: "--color-status-unhealthy",
    dotClassName: "bg-status-unhealthy",
    badgeClassName: "bg-status-unhealthy/15 text-status-unhealthy border-status-unhealthy/30",
  },
  STOPPED: {
    label: "Stopped", colorVar: "--color-status-stopped",
    dotClassName: "bg-status-stopped",
    badgeClassName: "bg-status-stopped/15 text-status-stopped border-status-stopped/30",
  },
  RESTARTING: {
    label: "Restarting", colorVar: "--color-status-restarting",
    dotClassName: "bg-status-restarting",
    badgeClassName: "bg-status-restarting/15 text-status-restarting border-status-restarting/30",
  },
  UNKNOWN: {
    label: "Unknown", colorVar: "--color-status-unknown",
    dotClassName: "bg-status-unknown",
    badgeClassName: "bg-status-unknown/15 text-status-unknown border-status-unknown/30",
  },
};

const UNKNOWN_HEALTH_STYLE: StatusStyle = HEALTH_STYLES.UNKNOWN;

/** Never throws on a status string the frontend doesn't recognize --
 * an unexpected value renders as UNKNOWN's style rather than crashing
 * the page, since the backend is the source of truth for what statuses
 * exist. */
export function healthStyle(status: string): StatusStyle {
  return HEALTH_STYLES[status as HealthStatus] ?? { ...UNKNOWN_HEALTH_STYLE, label: status };
}

export type DoctorCheckStatus = "PASS" | "WARN" | "FAIL" | "SKIP";

const DOCTOR_STYLES: Record<DoctorCheckStatus, StatusStyle> = {
  PASS: {
    label: "Pass", colorVar: "--color-status-healthy",
    dotClassName: "bg-status-healthy",
    badgeClassName: "bg-status-healthy/15 text-status-healthy border-status-healthy/30",
  },
  WARN: {
    label: "Warn", colorVar: "--color-status-degraded",
    dotClassName: "bg-status-degraded",
    badgeClassName: "bg-status-degraded/15 text-status-degraded border-status-degraded/30",
  },
  FAIL: {
    label: "Fail", colorVar: "--color-status-unhealthy",
    dotClassName: "bg-status-unhealthy",
    badgeClassName: "bg-status-unhealthy/15 text-status-unhealthy border-status-unhealthy/30",
  },
  SKIP: {
    label: "Skip", colorVar: "--color-status-unknown",
    dotClassName: "bg-status-unknown",
    badgeClassName: "bg-status-unknown/15 text-status-unknown border-status-unknown/30",
  },
};

export function doctorCheckStyle(status: string): StatusStyle {
  return DOCTOR_STYLES[status as DoctorCheckStatus] ?? { ...UNKNOWN_HEALTH_STYLE, label: status };
}

export type EvidenceSeverity = "info" | "warning" | "high" | "critical";

const SEVERITY_STYLES: Record<EvidenceSeverity, StatusStyle> = {
  info: {
    label: "Info", colorVar: "--color-severity-info",
    dotClassName: "bg-severity-info",
    badgeClassName: "bg-severity-info/15 text-severity-info border-severity-info/30",
  },
  warning: {
    label: "Warning", colorVar: "--color-severity-warning",
    dotClassName: "bg-severity-warning",
    badgeClassName: "bg-severity-warning/15 text-severity-warning border-severity-warning/30",
  },
  high: {
    label: "High", colorVar: "--color-severity-high",
    dotClassName: "bg-severity-high",
    badgeClassName: "bg-severity-high/15 text-severity-high border-severity-high/30",
  },
  critical: {
    label: "Critical", colorVar: "--color-severity-critical",
    dotClassName: "bg-severity-critical",
    badgeClassName: "bg-severity-critical/15 text-severity-critical border-severity-critical/30",
  },
};

export function severityStyle(severity: string): StatusStyle {
  return SEVERITY_STYLES[severity as EvidenceSeverity] ?? {
    label: severity, colorVar: "--color-muted-foreground",
    dotClassName: "bg-muted-foreground",
    badgeClassName: "bg-muted text-muted-foreground border-border",
  };
}

export type IncidentStatus = "open" | "resolved";

const INCIDENT_STYLES: Record<IncidentStatus, StatusStyle> = {
  open: {
    label: "Open", colorVar: "--color-status-unhealthy",
    dotClassName: "bg-status-unhealthy",
    badgeClassName: "bg-status-unhealthy/15 text-status-unhealthy border-status-unhealthy/30",
  },
  resolved: {
    label: "Resolved", colorVar: "--color-status-healthy",
    dotClassName: "bg-status-healthy",
    badgeClassName: "bg-status-healthy/15 text-status-healthy border-status-healthy/30",
  },
};

export function incidentStatusStyle(status: string): StatusStyle {
  return INCIDENT_STYLES[status as IncidentStatus] ?? { ...UNKNOWN_HEALTH_STYLE, label: status };
}

// Milestone 16 -- a host's own connectivity, distinct from any
// application's HealthStatus (see `argus.domain.host.HostStatus`'s own
// docstring for why the two are never conflated).
export type HostStatus = "ONLINE" | "STALE" | "OFFLINE";

const HOST_STYLES: Record<HostStatus, StatusStyle> = {
  ONLINE: {
    label: "Online", colorVar: "--color-status-healthy",
    dotClassName: "bg-status-healthy",
    badgeClassName: "bg-status-healthy/15 text-status-healthy border-status-healthy/30",
  },
  STALE: {
    label: "Stale", colorVar: "--color-status-degraded",
    dotClassName: "bg-status-degraded",
    badgeClassName: "bg-status-degraded/15 text-status-degraded border-status-degraded/30",
  },
  OFFLINE: {
    label: "Offline", colorVar: "--color-status-unhealthy",
    dotClassName: "bg-status-unhealthy",
    badgeClassName: "bg-status-unhealthy/15 text-status-unhealthy border-status-unhealthy/30",
  },
};

export function hostStatusStyle(status: string): StatusStyle {
  return HOST_STYLES[status as HostStatus] ?? { ...UNKNOWN_HEALTH_STYLE, label: status };
}
