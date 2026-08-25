import { useEffect, useState } from "react";
import { fullTimestamp, relativeTime } from "@/lib/format";

/** "3s ago" text that live-updates every second, with the full
 * timestamp always available via the native `title` tooltip -- the one
 * place every page renders a timestamp (see the milestone's own
 * "Relative Time" section: never mutate the underlying API value,
 * only its display). */
export function RelativeTime({ iso, className }: { iso: string | null | undefined; className?: string }) {
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(id);
  }, []);

  return (
    <time dateTime={iso ?? undefined} title={fullTimestamp(iso)} className={className}>
      {relativeTime(iso, now)}
    </time>
  );
}
