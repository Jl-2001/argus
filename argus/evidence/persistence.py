"""Reconciles in-memory ``SignalCandidate``s against already-persisted
``log_signals`` rows and writes the result -- the one place in
``argus.evidence`` that depends on ``argus.store.repository``, mirroring
how ``argus.incidents.engine`` (not ``argus.collectors``) is the layer
that compares fresh domain values against persisted state and decides
what to write.

``argus.evidence.collector``/``argus.evidence.aggregator`` never import
``argus.store`` -- they only ever produce plain ``SignalCandidate``
values, with no idea whether the database already has a matching row.
Whether a candidate *extends* an existing ``log_signals`` row (this
tick's lines are simply a continuation of the same still-open
aggregation bucket) or *starts a new one* (the previous bucket's window
already closed, or nothing matching exists yet) is exactly the kind of
"compare against what the database already knows" decision this module
exists to make -- ``argus.store.repository`` itself never makes it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from argus.evidence.aggregator import SignalCandidate
from argus.store.repository import Repository

__all__ = ["persist_candidates"]


def persist_candidates(
    repository: Repository,
    candidates: list[SignalCandidate],
    *,
    application_id: int,
    container_row_id: int,
    source_type: str,
    source_ref: str,
    aggregation_window_seconds: int,
) -> int:
    """Persist ``candidates`` (already aggregated in-memory for this
    tick), extending a matching still-open ``log_signals`` row from a
    *previous* tick when one exists and is still within its aggregation
    window, or inserting a new row otherwise.

    A candidate only ever extends the single most-recently-updated
    persisted row for its exact ``(container, category, signature)``
    key -- never an older, already-closed-out row for the same key, so
    a signal's row never grows to span more than
    ``aggregation_window_seconds`` from its own ``first_seen_at``,
    across ticks the same way ``argus.evidence.aggregator`` already
    guarantees within a single tick.

    Returns the number of candidates persisted (created or extended).
    """

    window = timedelta(seconds=aggregation_window_seconds)
    persisted = 0

    for candidate in candidates:
        existing = repository.find_latest_log_signal(
            container_row_id=container_row_id,
            category=candidate.category.value,
            normalized_signature=candidate.normalized_signature,
        )

        if existing is not None and (candidate.first_seen_at - existing.first_seen_at) <= window:
            repository.extend_log_signal(
                existing.id,
                last_seen_at=max(existing.last_seen_at, candidate.last_seen_at),
                additional_count=candidate.count,
            )
        else:
            repository.insert_log_signal(
                application_id=application_id,
                container_row_id=container_row_id,
                category=candidate.category.value,
                severity=candidate.severity.value,
                normalized_signature=candidate.normalized_signature,
                first_seen_at=candidate.first_seen_at,
                last_seen_at=candidate.last_seen_at,
                count=candidate.count,
                sample=candidate.sample,
                source_type=source_type,
                source_ref=source_ref,
            )
        persisted += 1

    return persisted
