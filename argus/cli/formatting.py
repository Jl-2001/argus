"""Human-readable rendering: relative time, plain aligned tables, and
JSON-safe primitives. No queries, no staleness logic, no business rules
-- this module only turns already-decided values into text.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence

from argus.domain.models import HealthStatus

__all__ = [
    "relative_time",
    "iso",
    "status_text",
    "render_table",
    "render_kv",
    "EM_DASH",
]

EM_DASH = "—"

_KV_LABEL_WIDTH = 14


def relative_time(now: datetime, timestamp: Optional[datetime]) -> str:
    """"3s ago" / "2m ago" / "4h ago" / "1d ago", or an em dash for `None`.

    Takes `now` explicitly rather than reading the clock itself -- the
    same injected-clock discipline used everywhere else in Argus.
    Bucket boundaries are exclusive upper bounds (an age of exactly 60s
    reads as "1m ago", not "60s ago"); a `timestamp` after `now`
    (clock skew) clamps to "0s ago" rather than showing a negative age.
    """

    if timestamp is None:
        return EM_DASH

    age_seconds = max(0.0, (now - timestamp).total_seconds())
    if age_seconds < 60:
        return f"{int(age_seconds)}s ago"
    if age_seconds < 3600:
        return f"{int(age_seconds // 60)}m ago"
    if age_seconds < 86400:
        return f"{int(age_seconds // 3600)}h ago"
    return f"{int(age_seconds // 86400)}d ago"


def iso(timestamp: Optional[datetime]) -> Optional[str]:
    """UTC ISO8601 for JSON output -- never a relative-time string."""

    if timestamp is None:
        return None
    return timestamp.astimezone(timezone.utc).isoformat()


def status_text(status: Optional[HealthStatus]) -> str:
    return status.value if status is not None else EM_DASH


def render_kv(pairs: Sequence[tuple[str, str]]) -> str:
    """A small aligned "Label   value" block, one pair per line, using a
    fixed label width so unrelated blocks (e.g. the collector summary in
    `argus status`) still line up consistently regardless of which
    optional lines happen to be present."""

    return "\n".join(f"{label.ljust(_KV_LABEL_WIDTH)}{value}" for label, value in pairs)


def render_table(
    headers: Sequence[str], rows: Sequence[Sequence[str]], *, show_header: bool = True
) -> str:
    """A plain, left-aligned, space-padded table -- no external dependency.

    Column widths are derived from the widest cell (including the
    header, even when it isn't printed) in each column, with a 2-space
    gap between columns. `show_header=False` renders a header-less log
    (used by `argus history`'s chronological listing).
    """

    columns = len(headers)
    widths = [len(header) for header in headers]
    for row in rows:
        for index in range(columns):
            widths[index] = max(widths[index], len(row[index]))

    def format_row(cells: Sequence[str]) -> str:
        # last column isn't padded -- avoids trailing whitespace on every line
        return "  ".join(
            cell if index == columns - 1 else cell.ljust(widths[index])
            for index, cell in enumerate(cells)
        )

    lines = [format_row(headers)] if show_header else []
    lines.extend(format_row(row) for row in rows)
    return "\n".join(lines)
