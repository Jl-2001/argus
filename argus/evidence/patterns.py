"""The deterministic pattern library -- the only thing that decides
whether a (redacted) log line becomes evidence, and which category it
becomes.

No LLM, no embedding model, no semantic similarity: every rule here is
an explicit, version-controlled, testable regular expression. Patterns
are checked in ``DEFAULT_PATTERNS`` order and the *first* match wins --
the same "explicit precedence, first match wins" discipline already
used in ``argus.domain.health.evaluate_container_health`` and
``argus.collectors.docker_collector``'s Docker-state parsing. This
matters here because some categories are deliberately narrower/more
specific than others (e.g. ``db_connection_timeout`` before the broad
``generic_error`` catch-all), so order encodes precedence, not just a
list.

A line that matches nothing here is not evidence at all -- Argus does
not manufacture a "generic_error" for every unmatched line (that would
turn routine, uninteresting log output into noise); ``generic_error``
only fires for lines that *do* contain an actual error-shaped keyword
but don't match anything more specific.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from argus.domain.models import EvidenceCategory, EvidenceSeverity

__all__ = ["LogPattern", "DEFAULT_PATTERNS", "classify_line"]


@dataclass(frozen=True, slots=True)
class LogPattern:
    category: EvidenceCategory
    severity: EvidenceSeverity
    patterns: tuple[re.Pattern[str], ...]
    description: str = ""


def _compiled(*raw_patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in raw_patterns)


# --------------------------------------------------------------------------
# Default pattern library
#
# Each category's patterns are deliberately narrow enough not to collide
# with its neighbors (see test_evidence_patterns.py's own
# "different categories aren't merged" tests) -- e.g. `dependency_unavailable`
# matches generic upstream-service language ("service unavailable",
# "upstream connect error") while `db_connection_timeout` matches
# database-driver-specific language ("connection terminated", "ECONNREFUSED")
# so a database outage and a generic upstream outage are never folded
# into the same bucket, even though both are, in the end, "something is
# unreachable".
# --------------------------------------------------------------------------

DEFAULT_PATTERNS: tuple[LogPattern, ...] = (
    LogPattern(
        category=EvidenceCategory.DB_CONNECTION_TIMEOUT,
        severity=EvidenceSeverity.HIGH,
        description="A database client reported its connection was refused, terminated, or timed out.",
        patterns=_compiled(
            r"connection\s+terminated",
            r"connection\s+timed?\s*out",
            r"\bECONNREFUSED\b",
            r"could\s+not\s+connect\s+to\s+(?:server|database)",
            r"connection\s+refused",
        ),
    ),
    LogPattern(
        category=EvidenceCategory.AUTHENTICATION_FAILURE,
        severity=EvidenceSeverity.HIGH,
        description="A login, token, or credential check failed.",
        patterns=_compiled(
            r"authentication\s+failed",
            r"invalid\s+credentials",
            r"\b401\s+unauthorized\b",
            r"permission\s+denied\s+for\s+(?:user|database|relation)",
        ),
    ),
    LogPattern(
        category=EvidenceCategory.PORT_CONFLICT,
        severity=EvidenceSeverity.WARNING,
        description="A process could not bind because the port was already in use.",
        patterns=_compiled(
            r"address\s+already\s+in\s+use",
            r"bind:.*already\s+in\s+use",
            r"port\s+is\s+already\s+allocated",
        ),
    ),
    LogPattern(
        category=EvidenceCategory.MISSING_ENVIRONMENT_VARIABLE,
        severity=EvidenceSeverity.HIGH,
        description="Startup failed because a required environment variable was absent.",
        patterns=_compiled(
            r"environment\s+variable\s+\S+\s+(?:is\s+)?(?:not\s+set|is\s+required|missing)",
            r"missing\s+required\s+environment\s+variable",
        ),
    ),
    LogPattern(
        category=EvidenceCategory.HTTP_5XX,
        severity=EvidenceSeverity.HIGH,
        description="An HTTP response in the 5xx range was logged.",
        patterns=_compiled(
            r'"\s*5\d{2}\s',  # common access-log shape: "GET / HTTP/1.1" 500
            r"\bstatus(?:_code)?[=:]\s*5\d{2}\b",
            r"http\s+status\s+5\d{2}\b",
            r"responded\s+with\s+5\d{2}\b",
        ),
    ),
    LogPattern(
        category=EvidenceCategory.OOM,
        severity=EvidenceSeverity.CRITICAL,
        description="The kernel or runtime reported an out-of-memory condition.",
        patterns=_compiled(
            r"out\s+of\s+memory",
            r"oom.?killed",
            r"cannot\s+allocate\s+memory",
            r"killed\s+process\s+\d+",
        ),
    ),
    LogPattern(
        category=EvidenceCategory.DISK_PRESSURE,
        severity=EvidenceSeverity.CRITICAL,
        description="The filesystem is out of (or nearly out of) space.",
        patterns=_compiled(
            r"no\s+space\s+left\s+on\s+device",
            r"disk\s+quota\s+exceeded",
            r"\bENOSPC\b",
        ),
    ),
    LogPattern(
        category=EvidenceCategory.NETWORK_FAILURE,
        severity=EvidenceSeverity.WARNING,
        description="A lower-level network failure was reported (not a specific dependency's outage).",
        patterns=_compiled(
            r"network\s+is\s+unreachable",
            r"\bENETUNREACH\b",
            r"\bEHOSTUNREACH\b",
            r"no\s+route\s+to\s+host",
        ),
    ),
    LogPattern(
        category=EvidenceCategory.DEPENDENCY_UNAVAILABLE,
        severity=EvidenceSeverity.HIGH,
        description="A generic upstream/dependency service was reported unreachable (not the database driver specifically).",
        patterns=_compiled(
            r"service\s+unavailable",
            r"upstream\s+connect\s+error",
            r"failed\s+to\s+connect\s+to\s+upstream",
            r"dial\s+tcp.*connection\s+refused",
        ),
    ),
    LogPattern(
        category=EvidenceCategory.GENERIC_ERROR,
        severity=EvidenceSeverity.INFO,
        description="A generic error-shaped keyword with no more specific category -- checked last, on purpose.",
        patterns=_compiled(
            r"\berror\b",
            r"\bexception\b",
            r"\bfatal\b",
            r"\btraceback\b",
        ),
    ),
)


def classify_line(redacted_text: str) -> Optional[LogPattern]:
    """Return the first ``LogPattern`` whose patterns match
    ``redacted_text``, or ``None`` if nothing matches -- meaning the
    line is not evidence at all.

    Matches against already-redacted text; a secret masked as
    ``[REDACTED]`` cannot itself accidentally match any pattern here
    (none of the rules above look for that literal string), so
    redaction never interferes with classification.

    ``container_restart`` and ``container_unhealthy`` are deliberately
    absent from ``DEFAULT_PATTERNS`` -- they are derived directly from
    Docker facts (restart_count deltas, health status), never from log
    text, and are constructed directly by
    ``argus.evidence.collector.collect_docker_fact_evidence``.
    """

    for pattern in DEFAULT_PATTERNS:
        if any(regex.search(redacted_text) for regex in pattern.patterns):
            return pattern
    return None
