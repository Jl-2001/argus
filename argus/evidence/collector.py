"""Per-container evidence collection: Docker logs in, structured
``SignalCandidate`` reconciliation decisions out.

This is the one place in ``argus.evidence`` that talks to Docker (via
the same read-only ``argus.collectors.docker_client.DockerClient``
Milestone 3 already built -- no second Docker client, no ``docker
exec``, no shelling out to the ``docker`` CLI) *and* the one place that
decides whether a new candidate extends an existing persisted
``log_signals`` row or starts a fresh one. Redaction, classification,
and time-bucketing themselves stay in ``argus.evidence.redaction`` /
``argus.evidence.patterns`` / ``argus.evidence.aggregator`` -- this
module only wires them together per container, applies the collection
limits, and isolates one container's failure from every other's.

Nothing here decides *when* to run (that is
``argus.collector.loop.CollectorLoop``'s job) or which incidents a
resulting signal is near (``argus.evidence.association``'s job).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from argus.collectors.docker_client import ContainerVanishedError, DockerClient, DockerUnavailableError
from argus.domain.models import EvidenceCategory, EvidenceSeverity
from argus.evidence.aggregator import (
    DEFAULT_AGGREGATION_WINDOW_SECONDS,
    ClassifiedLine,
    SignalCandidate,
    aggregate_classified_lines,
)
from argus.evidence.patterns import classify_line
from argus.evidence.redaction import redact_secrets

__all__ = [
    "EvidenceCollectionLimits",
    "DEFAULT_EVIDENCE_LIMITS",
    "ContainerEvidenceResult",
    "collect_evidence_for_container",
    "docker_fact_evidence",
]

logger = logging.getLogger(__name__)

# Docker's own per-line log timestamp prefix, e.g.
# "2026-08-22T10:00:00.123456789Z ". Docker always writes RFC3339 with a
# 9-digit (nanosecond) fraction when `timestamps=True` is requested,
# followed by exactly one space before the actual log content -- but this
# tolerates 0-9 fractional digits (mirroring
# argus.collectors.docker_collector's own timestamp regex) in case a log
# driver ever emits fewer.
_LOG_LINE_TIMESTAMP_RE = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(?P<frac>\d+))?Z (?P<text>.*)$"
)


@dataclass(frozen=True, slots=True)
class EvidenceCollectionLimits:
    """Bounds on how much log Argus ever reads/stores per container per
    tick. Deliberately conservative defaults -- evidence is meant to be
    a compact, queryable signal, never a log dump."""

    max_lines_per_container: int = 500
    max_bytes_per_container: int = 65_536
    max_sample_length: int = 300
    initial_lookback_seconds: int = 300
    aggregation_window_seconds: int = DEFAULT_AGGREGATION_WINDOW_SECONDS
    #: A tick-wide cap across every container combined -- see
    #: ``argus.collector.loop.CollectorLoop``'s evidence integration for
    #: how this is enforced (by skipping remaining containers for the
    #: rest of the tick, cursors untouched, never by silently dropping
    #: already-read evidence).
    max_signals_per_tick: int = 200


DEFAULT_EVIDENCE_LIMITS = EvidenceCollectionLimits()


@dataclass(frozen=True, slots=True)
class ContainerEvidenceResult:
    """What evidence collection found for one container this tick.

    ``error`` being set means this one container's log read failed and
    was skipped -- it never means the whole tick failed; see
    ``argus.collector.loop``'s integration of this module for the
    isolation guarantee.
    """

    container_id: str
    candidates: tuple[SignalCandidate, ...]
    new_cursor_at: Optional[datetime]
    lines_read: int
    error: Optional[str]


def _parse_log_line(raw_line: str) -> Optional[tuple[datetime, str]]:
    """Split one raw Docker log line into ``(observed_at, text)``, or
    ``None`` if it doesn't match Docker's own timestamp-prefixed shape
    (malformed/unexpected output is skipped, never guessed at)."""

    match = _LOG_LINE_TIMESTAMP_RE.match(raw_line)
    if match is None:
        return None

    base = match.group("base")
    frac = match.group("frac")
    text = match.group("text")
    if frac:
        microseconds = frac[:6].ljust(6, "0")
        normalized = f"{base}.{microseconds}+00:00"
    else:
        normalized = f"{base}+00:00"

    try:
        observed_at = datetime.fromisoformat(normalized).astimezone(timezone.utc)
    except ValueError:
        return None
    return observed_at, text


def collect_evidence_for_container(
    client: DockerClient,
    container_id: str,
    *,
    cursor_after: Optional[datetime],
    tick_at: datetime,
    limits: EvidenceCollectionLimits = DEFAULT_EVIDENCE_LIMITS,
) -> ContainerEvidenceResult:
    """Read, redact, classify, and aggregate one container's new log
    lines since ``cursor_after`` (or the last ``initial_lookback_seconds``
    if this container has never been read before).

    Any Docker-level failure for *this* container (unreachable daemon at
    the moment of the read, or the container having vanished) is caught
    here and reported via ``ContainerEvidenceResult.error`` -- it is
    never raised out of this function, so one unreadable container's log
    stream can never take down evidence collection for every other
    container in the same tick. Comparing this to
    ``argus.collectors.docker_collector.discover``'s own per-container
    isolation (a malformed container is recorded in ``skipped``, not
    raised) is deliberate: the same discipline, applied one layer over.
    """

    since = cursor_after if cursor_after is not None else tick_at - timedelta(
        seconds=limits.initial_lookback_seconds
    )

    try:
        raw_lines = client.get_logs(container_id, since=since, tail=limits.max_lines_per_container)
    except (DockerUnavailableError, ContainerVanishedError) as exc:
        logger.warning("evidence collection skipped for container %s: %s", container_id, exc)
        return ContainerEvidenceResult(
            container_id=container_id, candidates=(), new_cursor_at=None, lines_read=0, error=str(exc)
        )

    classified: list[ClassifiedLine] = []
    last_read_at: Optional[datetime] = None
    total_bytes = 0
    lines_read = 0

    for raw_line in raw_lines:
        if total_bytes >= limits.max_bytes_per_container:
            break

        parsed = _parse_log_line(raw_line)
        if parsed is None:
            continue
        observed_at, text = parsed

        if cursor_after is not None and observed_at <= cursor_after:
            # Strict dedup boundary -- Docker's own `since` filtering may
            # be second-precision only; this re-checks the exact,
            # nanosecond-precision line timestamp so a line already read
            # on a previous tick is never counted or classified again.
            continue

        lines_read += 1
        total_bytes += len(raw_line.encode("utf-8"))
        last_read_at = observed_at if last_read_at is None else max(last_read_at, observed_at)

        redacted = redact_secrets(text)
        pattern = classify_line(redacted)
        if pattern is None:
            continue  # not evidence -- an ordinary line is never stored

        classified.append(
            ClassifiedLine(
                observed_at=observed_at,
                category=pattern.category,
                severity=pattern.severity,
                redacted_text=redacted[: limits.max_sample_length],
            )
        )

    candidates = aggregate_classified_lines(
        classified, aggregation_window_seconds=limits.aggregation_window_seconds
    )

    return ContainerEvidenceResult(
        container_id=container_id,
        candidates=tuple(candidates),
        new_cursor_at=last_read_at,
        lines_read=lines_read,
        error=None,
    )


def docker_fact_evidence(
    *,
    observed_at: datetime,
    restart_count_before: Optional[int],
    restart_count_after: int,
    docker_health_is_unhealthy: bool,
) -> list[SignalCandidate]:
    """Evidence derived directly from Docker facts Argus already has on
    hand this tick -- never from log text. Covers the two categories
    ``argus.evidence.patterns`` deliberately never matches on log lines:
    ``container_restart`` (a real restart_count increase) and
    ``container_unhealthy`` (Docker's own health check reporting
    unhealthy).

    ``restart_count_before=None`` (no prior observation to compare
    against, e.g. a brand-new container) never produces
    ``container_restart`` evidence -- there is nothing to compare a
    single reading against, and a first-ever restart_count of, say, 3 is
    not evidence of three *new* restarts happening now.
    """

    candidates: list[SignalCandidate] = []

    if restart_count_before is not None and restart_count_after > restart_count_before:
        delta = restart_count_after - restart_count_before
        candidates.append(
            SignalCandidate(
                category=EvidenceCategory.CONTAINER_RESTART,
                severity=EvidenceSeverity.WARNING,
                normalized_signature=f"restart_count increased by {delta}",
                first_seen_at=observed_at,
                last_seen_at=observed_at,
                count=1,
                sample=(
                    f"restart_count increased from {restart_count_before} to {restart_count_after}"
                ),
            )
        )

    if docker_health_is_unhealthy:
        candidates.append(
            SignalCandidate(
                category=EvidenceCategory.CONTAINER_UNHEALTHY,
                severity=EvidenceSeverity.HIGH,
                normalized_signature="docker health check reports unhealthy",
                first_seen_at=observed_at,
                last_seen_at=observed_at,
                count=1,
                sample="Docker health check reports unhealthy",
            )
        )

    return candidates
