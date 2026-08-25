/**
 * The one shared timestamp formatter. API timestamps are always UTC
 * ISO 8601 strings (see the backend's `argus.cli.formatting.iso`) --
 * this module only ever *displays* them differently; it never mutates
 * or re-derives the underlying value the API sent.
 */

/** "3s ago" / "4m ago" / "2h ago" / "5d ago", or an em dash for
 * `null`/`undefined`/an unparseable string. Mirrors the backend CLI's
 * own `argus.cli.formatting.relative_time` bucketing exactly (same
 * exclusive upper bounds), so the dashboard never disagrees with
 * `argus status` about what "3s ago" means. */
export function relativeTime(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "—";
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "—";

  const ageSeconds = Math.max(0, (now.getTime() - then.getTime()) / 1000);
  if (ageSeconds < 60) return `${Math.floor(ageSeconds)}s ago`;
  if (ageSeconds < 3600) return `${Math.floor(ageSeconds / 60)}m ago`;
  if (ageSeconds < 86400) return `${Math.floor(ageSeconds / 3600)}h ago`;
  return `${Math.floor(ageSeconds / 86400)}d ago`;
}

/** The full, unambiguous timestamp for a tooltip/title attribute --
 * relative time is for scanning, this is for verifying. */
export function fullTimestamp(iso: string | null | undefined): string {
  if (!iso) return "unknown";
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return iso;
  return then.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    timeZoneName: "short",
  });
}

/** A compact HH:MM:SS for timeline rows -- still backed by the real
 * timestamp (via `title`), just a denser display form. */
export function shortTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "—";
  return then.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
