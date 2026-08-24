"""Deterministic aggregation: turn a run of matching, already-classified
log lines into a small number of structured evidence candidates instead
of one row per line.

Aggregation key: ``(container_id, category, normalized_signature)`` --
*not* including a raw timestamp, so structurally-identical repeated
events collapse together. What keeps the resulting rows bounded over a
long-running, still-failing container is the separate *time bucket*
dimension: a candidate only ever spans at most
``aggregation_window_seconds`` from its own first line to its last: a
matching line arriving later than that from the same (container,
category, signature) starts a *new* candidate rather than extending the
old one forever. This is a rolling window anchored to each candidate's
own ``first_seen_at`` -- not a fixed wall-clock-aligned bucket (e.g.
"every 5 minutes on the clock") -- so a burst that starts at 12:41:58
buckets from 12:41:58, not from whatever 5-minute mark happens to
contain it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from argus.domain.models import EvidenceCategory, EvidenceSeverity

__all__ = [
    "DEFAULT_AGGREGATION_WINDOW_SECONDS",
    "ClassifiedLine",
    "SignalCandidate",
    "normalize_signature",
    "aggregate_classified_lines",
]

DEFAULT_AGGREGATION_WINDOW_SECONDS = 300  # 5 minutes

_DIGIT_RUN = re.compile(r"\d+")
_HEX_ID = re.compile(r"\b[0-9a-fA-F]{8,}\b")
_WHITESPACE = re.compile(r"\s+")
_MAX_SIGNATURE_LENGTH = 200


def normalize_signature(redacted_text: str) -> str:
    """Collapse volatile substrings (ids, numbers, whitespace) so
    structurally-identical lines that differ only in incidental detail
    (a port number, a request id, a duration) produce the same
    signature. Deterministic and regex-based -- no hashing, no
    similarity scoring, nothing an AI would call "semantic".

    e.g. both of these normalize to the same signature::

        "connection to postgres:5432 timed out after 30s"
        "connection to postgres:5432 timed out after 45s"
    """

    text = redacted_text.strip().lower()
    text = _HEX_ID.sub("<id>", text)
    text = _DIGIT_RUN.sub("#", text)
    text = _WHITESPACE.sub(" ", text)
    return text[:_MAX_SIGNATURE_LENGTH]


@dataclass(frozen=True, slots=True)
class ClassifiedLine:
    """One log line that has already been redacted and matched against
    ``argus.evidence.patterns.DEFAULT_PATTERNS`` -- the aggregator's own
    input. Never holds unredacted text."""

    observed_at: datetime
    category: EvidenceCategory
    severity: EvidenceSeverity
    redacted_text: str


@dataclass(frozen=True, slots=True)
class SignalCandidate:
    """One aggregated evidence candidate, not yet reconciled against any
    persisted ``log_signals`` row -- see ``argus.evidence.collector``,
    which does that reconciliation (extend an existing row vs. insert a
    new one)."""

    category: EvidenceCategory
    severity: EvidenceSeverity
    normalized_signature: str
    first_seen_at: datetime
    last_seen_at: datetime
    count: int
    sample: str  # the *first* line's redacted text -- a stable, representative example


def aggregate_classified_lines(
    lines: Sequence[ClassifiedLine],
    *,
    aggregation_window_seconds: int = DEFAULT_AGGREGATION_WINDOW_SECONDS,
) -> list[SignalCandidate]:
    """Group ``lines`` (assumed already in chronological order) into
    ``SignalCandidate``s.

    Two passes, both deterministic:

    1. Group by ``(category, normalized_signature)``, preserving
       arrival order within each group.
    2. Within each group, split into consecutive time-buckets: a new
       bucket starts whenever a line's ``observed_at`` is more than
       ``aggregation_window_seconds`` after the *current* bucket's
       first line -- so no single candidate ever spans more than that
       window, regardless of how sparse or dense the tick schedule is.
    """

    window = timedelta(seconds=aggregation_window_seconds)

    groups: dict[tuple[EvidenceCategory, str], list[ClassifiedLine]] = {}
    order: list[tuple[EvidenceCategory, str]] = []
    for line in lines:
        signature = normalize_signature(line.redacted_text)
        key = (line.category, signature)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(line)

    candidates: list[SignalCandidate] = []
    for key in order:
        group_lines = groups[key]
        bucket: list[ClassifiedLine] = []
        for line in group_lines:
            if bucket and (line.observed_at - bucket[0].observed_at) > window:
                candidates.append(_bucket_to_candidate(bucket, key[1]))
                bucket = []
            bucket.append(line)
        if bucket:
            candidates.append(_bucket_to_candidate(bucket, key[1]))

    return candidates


def _bucket_to_candidate(bucket: list[ClassifiedLine], signature: str) -> SignalCandidate:
    first = bucket[0]
    last = bucket[-1]
    return SignalCandidate(
        category=first.category,
        severity=first.severity,
        normalized_signature=signature,
        first_seen_at=first.observed_at,
        last_seen_at=last.observed_at,
        count=len(bucket),
        sample=first.redacted_text,
    )
