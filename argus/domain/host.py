"""Host identity and connectivity vocabulary -- Milestone 16 (Secure
Multi-Host Agent Architecture).

Mirrors the split the rest of ``argus.domain`` already draws: this
module defines *shapes and pure rules*, never a database, an HTTP
client, or a Docker connection. A ``Host`` is "which machine did this
fact come from" -- deliberately not folded into ``Container``/
``Service``/``Application`` (see ``argus.ingestion.pipeline`` for how
those stay host-*agnostic* objects that get host-*scoped* only at the
persistence boundary).

``HostStatus`` is intentionally its own enum, not a reuse of
``argus.domain.models.HealthStatus`` -- "is this host's agent still
checking in" is a connectivity question, not a container/service/
application health question, and conflating the two would make a
future six-way ``HealthStatus`` match need to account for a concept it
was never about. See the Milestone 16 spec's own "Do not reuse
HealthStatus if host connectivity is conceptually different" note.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "LOCAL_HOST_KEY",
    "HostStatus",
    "evaluate_host_status",
    "scope_application_key",
    "ONLINE_THRESHOLD_MULTIPLIER",
    "STALE_THRESHOLD_MULTIPLIER",
]

#: The synthetic host every pre-Milestone-16 (and every ordinary
#: single-machine) observation belongs to. Never prefixed onto an
#: application key (see ``scope_application_key``) -- this is exactly
#: what keeps a single-host installation's application keys byte-for-
#: byte unchanged across this migration.
LOCAL_HOST_KEY = "local"


class HostStatus(str, Enum):
    """A host's own connectivity state -- independent of the health of
    anything it's monitoring. A host can be ONLINE while every
    application on it is UNHEALTHY, and OFFLINE while everything it
    last reported was HEALTHY -- the two questions don't correlate."""

    ONLINE = "ONLINE"
    STALE = "STALE"
    OFFLINE = "OFFLINE"


#: host considered ONLINE if its last heartbeat is within this many
#: multiples of its own poll interval; STALE up to
#: STALE_THRESHOLD_MULTIPLIER, OFFLINE beyond that. Matches the
#: Milestone 16 spec's own example (<=2x / >2x / >5x) exactly.
ONLINE_THRESHOLD_MULTIPLIER = 2
STALE_THRESHOLD_MULTIPLIER = 5


def evaluate_host_status(*, last_seen_at, now, poll_interval_seconds: float) -> HostStatus:
    """Pure, deterministic connectivity classification -- no persistence,
    no clock read (``now`` is always supplied by the caller, same
    discipline as every other evaluator in this package).

    ``last_seen_at`` in the future (clock skew, or a caller passing the
    wrong ``now``) is treated as "just seen" (ONLINE), never as a
    negative age -- this function never raises on that, it just can't
    tell the difference between "perfectly fresh" and "slightly ahead";
    ``argus.api.routes.agents`` is where real clock-skew *rejection*
    happens, before a snapshot is ever persisted.
    """

    age_seconds = max((now - last_seen_at).total_seconds(), 0.0)
    if age_seconds <= ONLINE_THRESHOLD_MULTIPLIER * poll_interval_seconds:
        return HostStatus.ONLINE
    if age_seconds <= STALE_THRESHOLD_MULTIPLIER * poll_interval_seconds:
        return HostStatus.STALE
    return HostStatus.OFFLINE


def scope_application_key(host_key: str, local_key: str) -> str:
    """The one place an application's *local* key (what the collector on
    that host actually calls it, e.g. ``"cnstrct"``) becomes its
    *globally* unique, persisted key.

    The local host is left completely unprefixed -- this is the whole
    backward-compatibility mechanism for Milestone 16: every existing
    single-host database's application keys are already, by
    construction, ``scope_application_key("local", key) == key``, so
    nothing about them needs to change. Any other host gets its
    ``host_key`` prefixed on, exactly matching the spec's own example
    (``dell:cnstrct`` vs. plain ``cnstrct``) -- this is what lets two
    hosts run identically-named compose projects without their
    identities colliding, without ever having to widen
    ``applications.key``'s own UNIQUE constraint.
    """

    if host_key == LOCAL_HOST_KEY:
        return local_key
    return f"{host_key}:{local_key}"
