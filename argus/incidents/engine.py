"""Deterministic transition detection and application-level incident lifecycle.

Turns the health values discovery/evaluation already computed for one
tick into persisted state transitions (container, service, and
application scope) and, from application-scope transitions alone,
opens/escalates/resolves incidents.

This module never calls Docker, never reads the clock (``occurred_at``
is always supplied by the caller -- the tick's own timestamp), never
sends a notification, and never calls an AI model. It also never
recomputes health -- ``container_statuses`` and each ``Application``'s
own ``derived_status``/``Service.derived_status`` are values the
domain/health layers (via ``discover()``) already produced; this
module only compares them against what the database says came before.

Everything the engine needs to know about "what happened last" comes
from the database, not from memory -- there is no cached prior state
anywhere in this module. That is what makes an Argus restart safe: the
very next tick's comparison is against the same persisted row a
long-running process would have compared against anyway.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from argus.domain.models import Application, HealthStatus
from argus.store.database import PersistenceError
from argus.store.repository import Repository

__all__ = [
    "IncidentProcessingError",
    "IncidentProcessingResult",
    "TransitionOccurred",
    "IncidentOpened",
    "IncidentUpdated",
    "IncidentResolved",
    "incident_severity_rank",
    "process_transitions_and_incidents",
]

logger = logging.getLogger(__name__)


class IncidentProcessingError(PersistenceError):
    """Transition/incident processing for this tick could not be trusted.

    A subclass of ``argus.store.database.PersistenceError`` on purpose:
    the collector loop already treats a ``PersistenceError`` from
    ``persist_discovery`` as "fail this tick, do not record success, keep
    the daemon running" -- transition/incident failures get exactly the
    same treatment, for exactly the same reason, with no new exception
    category the loop needs to learn about.
    """


@dataclass(frozen=True, slots=True)
class TransitionOccurred:
    """One committed container/service/application transition from this
    tick -- Milestone 15's realtime layer turns these into
    `*.status_changed` events, after `process_transitions_and_incidents`'s
    own transaction has already committed (see that function's own
    docstring on transaction boundaries; nothing here is ever built from
    an uncommitted write)."""

    scope: str  # "application" | "service" | "container"
    scope_id: int
    application_key: str
    from_status: HealthStatus | None
    to_status: HealthStatus
    transition_id: int
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class IncidentOpened:
    incident_id: int
    application_key: str
    opening_status: HealthStatus


@dataclass(frozen=True, slots=True)
class IncidentUpdated:
    """Emitted only on a genuine `worst_status` escalation -- never once
    per tick an already-open incident merely continues (see
    `_update_incident_lifecycle`'s own "nothing to write" branch)."""

    incident_id: int
    application_key: str
    worst_status: HealthStatus


@dataclass(frozen=True, slots=True)
class IncidentResolved:
    incident_id: int
    application_key: str


@dataclass(frozen=True, slots=True)
class IncidentProcessingResult:
    """What happened while processing one tick's worth of applications.

    The four `*_created`/`*_opened`/`*_updated`/`*_resolved` counts are
    Milestone 6's original summary and remain exactly as they always
    were. The four `transitions`/`opened_incidents`/`updated_incidents`/
    `resolved_incidents` tuples are Milestone 15's additive detail --
    the same events, with the actual identifying data (scope/status/
    incident id/application key) a realtime consumer needs to build an
    event payload, rather than just a number to log.
    """

    transitions_created: int
    incidents_opened: int
    incidents_updated: int
    incidents_resolved: int
    transitions: tuple[TransitionOccurred, ...] = ()
    opened_incidents: tuple[IncidentOpened, ...] = ()
    updated_incidents: tuple[IncidentUpdated, ...] = ()
    resolved_incidents: tuple[IncidentResolved, ...] = ()


class _Stats:
    """Mutable accumulator, private to one `process_transitions_and_incidents`
    call -- never exposed; the public result is the frozen snapshot above."""

    __slots__ = (
        "transitions_created", "incidents_opened", "incidents_updated", "incidents_resolved",
        "transitions", "opened_incidents", "updated_incidents", "resolved_incidents",
    )

    def __init__(self) -> None:
        self.transitions_created = 0
        self.incidents_opened = 0
        self.incidents_updated = 0
        self.incidents_resolved = 0
        self.transitions: list[TransitionOccurred] = []
        self.opened_incidents: list[IncidentOpened] = []
        self.updated_incidents: list[IncidentUpdated] = []
        self.resolved_incidents: list[IncidentResolved] = []


@dataclass(frozen=True, slots=True)
class _TransitionOutcome:
    changed: bool
    from_status: HealthStatus | None
    to_status: HealthStatus
    transition_id: int | None  # always set when changed is True
    occurred_at: datetime | None = None  # the actual timestamp used, when changed


# --------------------------------------------------------------------------
# Incident severity ranking
#
# This is a *different* ranking from Milestone 2's application-rollup
# severity (argus.domain.health), on purpose -- the two questions aren't
# the same one. Rollup asks "what is the worst thing happening across
# sibling services *right now*", and a partial STOPPED there is folded
# into UNHEALTHY before it ever reaches this module (see
# evaluate_application_health) -- so by the time an application-level
# STOPPED reaches the incident engine, it always means the *entire*
# application was down, never a partial stop. This ranking instead asks
# "how bad did this one incident get, over its whole lifetime", which is
# a question about one application's own status history, not about
# comparing siblings.
#
#   UNHEALTHY   -- actively failing while attempting to run: often the
#                  most urgent case (crash loops serving bad data, failed
#                  health checks under load), so it ranks worst.
#   STOPPED     -- the whole application is down. Serious, but a clean
#                  "off" is a simpler, more legible failure than an
#                  active malfunction, so it ranks just below UNHEALTHY.
#   RESTARTING  -- an in-progress recovery attempt; worse than merely
#                  DEGRADED but not yet a confirmed failure the way
#                  STOPPED/UNHEALTHY are.
#   DEGRADED    -- partially functioning; ranks above UNKNOWN because it
#                  is still *informative* (Argus knows it's slow/unwell),
#                  whereas UNKNOWN means Argus doesn't know what's
#                  happening at all -- matching Milestone 2's rollup,
#                  which treats "we don't know" as a real, if lesser,
#                  concern rather than the least severe outcome.
#   UNKNOWN     -- least severe of the bad states, for the reason above --
#                  but still bad enough to open/keep open an incident;
#                  see the module docstring on why UNKNOWN must never be
#                  silently treated as fine.
#
# A plain dict, never enum declaration order or string comparison -- see
# the architecture guard test for why that distinction is checked.
# --------------------------------------------------------------------------

_INCIDENT_SEVERITY_RANK: dict[HealthStatus, int] = {
    HealthStatus.UNHEALTHY: 5,
    HealthStatus.STOPPED: 4,
    HealthStatus.RESTARTING: 3,
    HealthStatus.DEGRADED: 2,
    HealthStatus.UNKNOWN: 1,
}


def incident_severity_rank(status: HealthStatus) -> int:
    """The incident-severity rank for a bad `HealthStatus`. Higher is worse.

    Raises `ValueError` for `HEALTHY` -- it is never a "how bad did this
    get" candidate; an incident's `worst_status` is only ever set while
    the incident is open, which by definition means the current status
    is not HEALTHY.
    """

    try:
        return _INCIDENT_SEVERITY_RANK[status]
    except KeyError:
        raise ValueError(
            f"{status!r} has no incident severity ranking -- only a bad status can be a "
            "worst_status candidate"
        ) from None


def _worse_of(a: HealthStatus, b: HealthStatus) -> HealthStatus:
    return a if incident_severity_rank(a) >= incident_severity_rank(b) else b


# --------------------------------------------------------------------------
# Transition detection
# --------------------------------------------------------------------------


def _find_true_occurred_at(
    repository: Repository,
    *,
    container_id: str,
    current_status: HealthStatus,
    since: datetime | None,
    fallback: datetime,
) -> datetime:
    """The earliest persisted observation for `container_id`, after `since`,
    that already shows `current_status` -- i.e. when the new status
    genuinely first appeared, rather than whenever a (possibly delayed)
    tick happens to notice it.

    This is what makes the following sequence correct: an Observation
    proving UNHEALTHY is persisted at T1, but transition processing for
    that tick fails before it can act on it; a later tick T2 finally
    succeeds. Without this lookup, the resulting transition would be
    timestamped T2 -- a real, observable inaccuracy, since Argus already
    had evidence of the new status at T1. `since` is the scope's own
    last recorded transition time (or `None` for a first-ever
    transition), so the search never reaches back past a status Argus
    already recorded as different.

    Deliberately bounded: this finds the earliest evidence of *this one*
    status change, not a full reconstruction of every distinct status the
    container may have passed through inside an unprocessed gap -- if the
    status genuinely flickered through several different values before
    settling, only the first occurrence of `current_status` is used.
    Replaying a gap's full history through rollup would be a real
    redesign of Milestone 6, not this hardening fix's job.

    Falls back to `fallback` (the calling tick's own timestamp) if no
    matching observation exists -- e.g. a synthetic caller that never
    persisted an Observation, or the ordinary case where the tick
    processing its own Observation succeeds immediately and that
    Observation's `observed_at` already equals `fallback` anyway.
    """

    for observation in repository.get_observations_after(container_id, after=since):
        if observation.derived_status is current_status:
            return observation.observed_at
    return fallback


def _detect_and_record_transition(
    repository: Repository,
    *,
    scope: str,
    scope_id: int,
    current_status: HealthStatus,
    occurred_at: datetime,
    container_id: str | None = None,
) -> _TransitionOutcome:
    """Compare `current_status` against the database's own last-known
    status for (scope, scope_id) and record a transition if it changed.

    "No prior transition row exists yet" (a brand new entity) is treated
    as `from_status=None`, and a `None -> current_status` transition is
    recorded exactly once, on the first tick that ever observes this
    entity -- because `current_status` is always a real `HealthStatus`,
    never `None`, so `None != current_status` always holds the first
    time. Every later tick's comparison is against the row this call
    itself just wrote, which is what makes a repeated identical status
    produce no further rows, restart or no restart: there is no
    in-memory state to lose.

    When `container_id` is supplied (container scope, and service scope
    reusing its single v0.1 container -- see `_process_application`),
    the transition's `occurred_at` is corrected via
    `_find_true_occurred_at` to the earliest persisted evidence of the
    new status, rather than blindly using the calling tick's own
    timestamp. Application scope has no single container to check
    against; its caller instead passes an already-corrected
    `occurred_at` directly (see `_process_application`).
    """

    last = repository.get_last_transition(scope=scope, scope_id=scope_id)
    previous_status = last.to_status if last is not None else None

    if previous_status == current_status:
        return _TransitionOutcome(
            changed=False, from_status=previous_status, to_status=current_status, transition_id=None
        )

    true_occurred_at = occurred_at
    if container_id is not None:
        true_occurred_at = _find_true_occurred_at(
            repository,
            container_id=container_id,
            current_status=current_status,
            since=last.occurred_at if last is not None else None,
            fallback=occurred_at,
        )

    transition_id = repository.insert_transition(
        scope=scope,
        scope_id=scope_id,
        from_status=previous_status,
        to_status=current_status,
        occurred_at=true_occurred_at,
    )
    logger.info(
        "%s %d transitioned: %s -> %s",
        scope,
        scope_id,
        previous_status.value if previous_status is not None else "NULL",
        current_status.value,
    )
    return _TransitionOutcome(
        changed=True,
        from_status=previous_status,
        to_status=current_status,
        transition_id=transition_id,
        occurred_at=true_occurred_at,
    )


# --------------------------------------------------------------------------
# Incident lifecycle (application scope only)
# --------------------------------------------------------------------------


def _update_incident_lifecycle(
    repository: Repository,
    *,
    application_key: str,
    application_row_id: int,
    outcome: _TransitionOutcome,
    occurred_at: datetime,
    stats: _Stats,
) -> None:
    """Only called when the application's own status just changed.

    ``f"application:{key}"`` is intentionally coarse -- it identifies
    *which application* has an ongoing bad episode, not which specific
    bad status caused it, so DEGRADED -> UNHEALTHY -> RESTARTING ->
    DEGRADED collapses into one incident rather than several.
    """

    failure_signature = f"application:{application_key}"

    if outcome.to_status is HealthStatus.HEALTHY:
        # HEALTHY is the only status that resolves an incident. If none is
        # open, there is nothing to do -- never fabricate one just to
        # close it (e.g. NULL -> HEALTHY on first-ever observation).
        open_incident = repository.get_open_incident(failure_signature=failure_signature)
        if open_incident is None:
            return
        repository.resolve_incident(
            incident_id=open_incident.id,
            closed_at=occurred_at,
            resolving_transition_id=outcome.transition_id,
        )
        stats.incidents_resolved += 1
        stats.resolved_incidents.append(IncidentResolved(incident_id=open_incident.id, application_key=application_key))
        logger.info("incident resolved: %s", failure_signature)
        return

    # outcome.to_status is one of DEGRADED / UNHEALTHY / RESTARTING /
    # STOPPED / UNKNOWN -- all incident-worthy in v0.1.
    open_incident = repository.get_open_incident(failure_signature=failure_signature)

    if open_incident is None:
        incident_id = repository.open_incident(
            scope_id=application_row_id,
            failure_signature=failure_signature,
            opened_at=occurred_at,
            opening_status=outcome.to_status,
            opening_transition_id=outcome.transition_id,
        )
        stats.incidents_opened += 1
        stats.opened_incidents.append(
            IncidentOpened(incident_id=incident_id, application_key=application_key, opening_status=outcome.to_status)
        )
        logger.info("incident opened: %s (%s)", failure_signature, outcome.to_status.value)
        return

    # Already open -- the application moved between bad states. Escalate
    # worst_status only if this one is actually worse; never downgrade it
    # (that's why UNHEALTHY -> DEGRADED keeps worst_status at UNHEALTHY).
    new_worst = _worse_of(open_incident.worst_status, outcome.to_status)
    if new_worst != open_incident.worst_status:
        repository.update_incident_worst_status(incident_id=open_incident.id, worst_status=new_worst)
        stats.incidents_updated += 1
        stats.updated_incidents.append(
            IncidentUpdated(incident_id=open_incident.id, application_key=application_key, worst_status=new_worst)
        )
        logger.info(
            "incident worst status updated: %s (%s -> %s)",
            failure_signature,
            open_incident.worst_status.value,
            new_worst.value,
        )
    # else: a real transition happened (e.g. UNHEALTHY -> DEGRADED) but it
    # isn't worse than what's already recorded -- nothing to write, and
    # deliberately no INFO log for it (see the module docstring on not
    # spamming logs for changes that aren't actually escalations).


def _record_transition_event(
    stats: _Stats, *, scope: str, scope_id: int, application_key: str, outcome: _TransitionOutcome
) -> None:
    """Appends a `TransitionOccurred` for one `changed` outcome -- shared
    by all three scopes' call sites in `_process_application` so there is
    one place this construction happens, not three."""

    assert outcome.changed and outcome.transition_id is not None and outcome.occurred_at is not None
    stats.transitions.append(
        TransitionOccurred(
            scope=scope, scope_id=scope_id, application_key=application_key,
            from_status=outcome.from_status, to_status=outcome.to_status,
            transition_id=outcome.transition_id, occurred_at=outcome.occurred_at,
        )
    )


def _process_application(
    repository: Repository,
    application: Application,
    container_statuses: Mapping[str, HealthStatus],
    occurred_at: datetime,
    stats: _Stats,
) -> None:
    application_record = repository.get_application(application.key)
    if application_record is None:
        raise IncidentProcessingError(
            f"application {application.key!r} was not found in the store immediately after "
            "being persisted; transition/incident processing cannot proceed"
        )

    changed_service_timestamps: list[datetime] = []

    for service in application.services:
        # v0.1 assumes exactly one container per service -- captured here so
        # the service-scope transition below can reuse the same
        # occurred_at correction as its lone container (they describe the
        # identical real-world event, not two independent ones).
        sole_container_id = service.containers[0].container_id if len(service.containers) == 1 else None

        for container in service.containers:
            container_record = repository.get_container_by_docker_id(container.container_id)
            if container_record is None:
                raise IncidentProcessingError(
                    f"container {container.container_id!r} was not found in the store "
                    "immediately after being persisted"
                )
            try:
                current_container_status = container_statuses[container.container_id]
            except KeyError:
                raise IncidentProcessingError(
                    f"no evaluated health status was supplied for container "
                    f"{container.container_id!r}"
                ) from None

            container_outcome = _detect_and_record_transition(
                repository,
                scope="container",
                scope_id=container_record.id,
                current_status=current_container_status,
                occurred_at=occurred_at,
                container_id=container.container_id,
            )
            if container_outcome.changed:
                stats.transitions_created += 1
                _record_transition_event(
                    stats, scope="container", scope_id=container_record.id,
                    application_key=application.key, outcome=container_outcome,
                )

        service_record = repository.get_service_by_key(
            application_id=application_record.id, compose_service=service.compose_service
        )
        if service_record is None:
            raise IncidentProcessingError(
                f"service {service.name!r} in application {application.key!r} was not found "
                "in the store immediately after being persisted"
            )

        # v0.1 assumes one container per service, so this currently mirrors
        # the single container's status -- but it is still recorded as its
        # own transition (not skipped), so container-scope and
        # service-scope history can later be queried independently, and so
        # that future replica-aware rollup only ever has to change how
        # `service.derived_status` is computed upstream, not whether it
        # gets persisted here.
        service_outcome = _detect_and_record_transition(
            repository,
            scope="service",
            scope_id=service_record.id,
            current_status=service.derived_status,
            occurred_at=occurred_at,
            container_id=sole_container_id,
        )
        if service_outcome.changed:
            stats.transitions_created += 1
            _record_transition_event(
                stats, scope="service", scope_id=service_record.id,
                application_key=application.key, outcome=service_outcome,
            )
            if service_outcome.occurred_at is not None:
                changed_service_timestamps.append(service_outcome.occurred_at)

    # Application scope has no single container of its own to check
    # observation history against. Instead, if one or more of its services
    # changed this tick, the application's own transition genuinely began
    # whenever the earliest of those (already-corrected) service changes
    # did -- so use that, rather than the tick's own timestamp, which would
    # repeat the exact T2-instead-of-T1 inaccuracy this correction exists
    # to avoid. Falls back to `occurred_at` (this tick) if no child service
    # outcome changed, e.g. the application's rollup rule itself is what
    # changed rather than any one service.
    application_occurred_at = min(changed_service_timestamps, default=occurred_at)

    application_outcome = _detect_and_record_transition(
        repository,
        scope="application",
        scope_id=application_record.id,
        current_status=application.derived_status,
        occurred_at=application_occurred_at,
    )
    if application_outcome.changed:
        stats.transitions_created += 1
        _record_transition_event(
            stats, scope="application", scope_id=application_record.id,
            application_key=application.key, outcome=application_outcome,
        )
        _update_incident_lifecycle(
            repository,
            application_key=application.key,
            application_row_id=application_record.id,
            outcome=application_outcome,
            occurred_at=occurred_at,
            stats=stats,
        )


def process_transitions_and_incidents(
    *,
    repository: Repository,
    applications: Sequence[Application],
    container_statuses: Mapping[str, HealthStatus],
    occurred_at: datetime,
) -> IncidentProcessingResult:
    """Detect transitions and update incidents for one tick's applications.

    ``applications`` are the same domain objects `persist_discovery` was
    just given -- their `derived_status`/`Service.derived_status` are
    already the real, evaluated values (Milestone 2/3), not recomputed
    here. ``container_statuses`` supplies the one piece those domain
    objects don't carry per-container (`Container` has no
    `derived_status` field of its own); it is keyed by Docker's
    `container_id`.

    All of it -- every container/service/application transition and
    every incident open/escalate/resolve for this tick -- runs inside
    one `repository.transaction()`: either the whole tick's transition
    and incident state is written coherently, or none of it is. This
    deliberately does *not* span the same transaction as
    `persist_discovery` (a separate, already-committed transaction) --
    see the Milestone 6 report for why that boundary was chosen.
    """

    stats = _Stats()
    with repository.transaction():
        for application in applications:
            _process_application(repository, application, container_statuses, occurred_at, stats)

    return IncidentProcessingResult(
        transitions_created=stats.transitions_created,
        incidents_opened=stats.incidents_opened,
        incidents_updated=stats.incidents_updated,
        incidents_resolved=stats.incidents_resolved,
        transitions=tuple(stats.transitions),
        opened_incidents=tuple(stats.opened_incidents),
        updated_incidents=tuple(stats.updated_incidents),
        resolved_incidents=tuple(stats.resolved_incidents),
    )
