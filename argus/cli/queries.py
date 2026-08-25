"""Read-model layer for the CLI.

This is where "what should Argus *currently* say about this entity"
lives -- specifically the staleness rule that keeps a quiet collector
from making stale data look confidently HEALTHY. It belongs neither in
``argus.store.repository`` (which must never decide anything about
health) nor in ``argus.cli.formatting`` (which only turns already-
decided values into text); it sits between them, calling `Repository`
methods exclusively -- never raw SQL -- and returning small, typed,
already-display-ready records.

Only the `HealthRules` *data* (a threshold constant) is imported from
``argus.domain.health`` -- never an ``evaluate_*`` function. Nothing
here reads the clock; every function takes `now` explicitly, read once
at the CLI's command boundary (`argus.cli.main`).
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from argus.domain.health import DEFAULT_HEALTH_RULES, HealthRules
from argus.domain.host import LOCAL_HOST_KEY, HostStatus, evaluate_host_status
from argus.domain.models import EvidenceCategory, EvidenceSeverity, HealthStatus
from argus.store.repository import (
    ApplicationCountsRecord,
    EvidenceRecord,
    IncidentWithApplicationRecord,
    Repository,
    TransitionHistoryRow,
)

__all__ = [
    "CollectorStatusView",
    "ApplicationSummary",
    "PortView",
    "ContainerDetail",
    "ServiceDetail",
    "ApplicationDetail",
    "EvidenceView",
    "get_collector_status",
    "list_application_summaries",
    "get_application_detail",
    "list_incidents",
    "list_history",
    "find_application_key",
    "suggest_application_name",
    "list_evidence_for_application",
    "list_evidence_for_incident",
]


# --------------------------------------------------------------------------
# Collector status
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CollectorStatusView:
    """The collector's own classified liveness, for `argus status`.

    ``data_is_fresh`` is the separate signal every other query in this
    module uses to decide whether a stored "current status" can be
    trusted at all -- see its own docstring below for why it is not
    the same check as `classification`.
    """

    classification: str  # "NEVER_RUN" | "STALE" | "FAILING" | "HEALTHY"
    last_tick_at: Optional[datetime]
    last_success_at: Optional[datetime]
    consecutive_failures: int
    last_error: Optional[str]
    data_is_fresh: bool


def get_collector_status(
    repository: Repository, *, now: datetime, rules: HealthRules = DEFAULT_HEALTH_RULES
) -> CollectorStatusView:
    """Classify the collector's liveness from `collector_state` alone.

    - `NEVER_RUN`: no tick has ever been attempted.
    - `STALE`: `last_tick_at` itself is old -- the collector *process*
      looks dead (no tick attempts at all recently), not just failing.
    - `FAILING`: ticks are happening (`last_tick_at` is recent) but the
      most recent ones aren't succeeding (`consecutive_failures > 0`) --
      e.g. Docker is down and the loop is backing off and retrying.
    - `HEALTHY`: ticking, and the last one succeeded.

    `data_is_fresh` is a *different* question, checked against
    `last_success_at` rather than `last_tick_at`: a `FAILING` collector
    keeps ticking (so `last_tick_at` stays recent) but isn't learning
    anything new, so `last_success_at` is the honest signal for "is
    Argus's knowledge of the world still current." Both checks reuse
    the same `rules.unknown_after` threshold Milestone 2 already
    defined for observation staleness -- one constant, reused twice,
    not two invented numbers.
    """

    state = repository.get_collector_state()

    if state.last_tick_at is None:
        classification = "NEVER_RUN"
    elif (now - state.last_tick_at).total_seconds() > rules.unknown_after:
        classification = "STALE"
    elif state.consecutive_failures > 0:
        classification = "FAILING"
    else:
        classification = "HEALTHY"

    data_is_fresh = state.last_success_at is not None and (
        (now - state.last_success_at).total_seconds() <= rules.unknown_after
    )

    return CollectorStatusView(
        classification=classification,
        last_tick_at=state.last_tick_at,
        last_success_at=state.last_success_at,
        consecutive_failures=state.consecutive_failures,
        last_error=state.last_error,
        data_is_fresh=data_is_fresh,
    )


def _current_status(
    repository: Repository, *, scope: str, scope_id: int, data_is_fresh: bool
) -> HealthStatus:
    """The status to *display* for one entity right now.

    If the collector's data isn't fresh (see `get_collector_status`),
    every entity reads as UNKNOWN regardless of its last recorded
    transition -- Argus genuinely doesn't know the current state of
    anything when its one collector has stopped learning, and a
    long-unchanged HEALTHY transition must never be presented as if it
    were being actively reconfirmed. This is the same "derive UNKNOWN
    from freshness" behavior the Milestone 5 report already named as
    the intended design, implemented here rather than invented anew.
    """

    if not data_is_fresh:
        return HealthStatus.UNKNOWN
    last = repository.get_last_transition(scope=scope, scope_id=scope_id)
    return last.to_status if last is not None else HealthStatus.UNKNOWN


# --------------------------------------------------------------------------
# Applications: list + detail
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApplicationSummary:
    key: str
    name: str
    status: HealthStatus
    service_count: int
    container_count: int
    last_seen_at: datetime
    #: Milestone 16. Defaulted so this dataclass's positional/keyword
    #: shape is unchanged for every pre-existing caller that never
    #: cared about hosts.
    host_key: str = LOCAL_HOST_KEY
    host_display_name: str = "Local Host"


def _host_freshness_map(
    repository: Repository, *, now: datetime, local_data_is_fresh: bool
) -> dict[str, bool]:
    """Milestone 16: whether an application's *owning host* currently
    counts as a trustworthy source of "current state", keyed by
    ``host_key``.

    Before this milestone, "is Argus's data fresh" was a single,
    global question answered once from the local `collector_state` row
    (`get_collector_status`) and applied uniformly to every
    application. That is no longer right once applications can come
    from different, independently-live hosts: a remote agent going
    silent must make *its own* applications read UNKNOWN (see the
    milestone's own "Remote Host Offline Incident" -- "its applications
    should surface UNKNOWN/stale through existing freshness
    semantics"), without that same silence affecting the local host's
    own, still-live applications, and vice versa.

    The local host keeps using the exact original signal
    (`local_data_is_fresh`, i.e. `collector_status.data_is_fresh`) --
    byte-for-byte the same rule a single-host installation has always
    used. A remote agent host is fresh exactly when
    `argus.domain.host.evaluate_host_status` currently classifies it
    ONLINE -- STALE/OFFLINE both mean "don't trust this host's last
    recorded transition as current", the same way a STALE/FAILING local
    collector already does.
    """

    freshness = {LOCAL_HOST_KEY: local_data_is_fresh}
    for host in repository.list_hosts():
        if host.host_key == LOCAL_HOST_KEY:
            continue
        status = evaluate_host_status(
            last_seen_at=host.last_seen_at, now=now, poll_interval_seconds=_HOST_STATUS_POLL_INTERVAL_SECONDS
        )
        freshness[host.host_key] = status == HostStatus.ONLINE
    return freshness


def list_application_summaries(
    repository: Repository, *, now: datetime, rules: HealthRules = DEFAULT_HEALTH_RULES
) -> list[ApplicationSummary]:
    """All applications, name ascending (`list_applications_with_counts`
    already orders by key) -- deterministic and stable across runs,
    rather than reordering by severity, which would make repeated
    invocations harder to read as a stable table."""

    collector_status = get_collector_status(repository, now=now, rules=rules)
    freshness_by_host = _host_freshness_map(repository, now=now, local_data_is_fresh=collector_status.data_is_fresh)
    return [
        ApplicationSummary(
            key=record.key,
            name=record.name,
            status=_current_status(
                repository, scope="application", scope_id=record.id,
                data_is_fresh=freshness_by_host.get(record.host_key, False),
            ),
            service_count=record.service_count,
            container_count=record.container_count,
            last_seen_at=record.last_seen_at,
            host_key=record.host_key,
            host_display_name=record.host_display_name,
        )
        for record in repository.list_applications_with_counts()
    ]


def find_application_key(repository: Repository, name_or_key: str) -> Optional[str]:
    """Case-insensitive lookup by name or key. Returns the canonical key,
    or None if nothing matches."""

    needle = name_or_key.strip().casefold()
    for record in repository.list_applications_with_counts():
        if record.key.casefold() == needle or record.name.casefold() == needle:
            return record.key
    return None


def suggest_application_name(repository: Repository, name_or_key: str) -> Optional[str]:
    """A "did you mean" suggestion via stdlib difflib -- no fuzzy-search
    dependency needed for a handful of application names.

    Only ever suggests a display `name`, never the internal `key` --
    matching case-insensitively against both (see `find_application_key`)
    is right for *accepting* input, but recommending a user retype an
    internal identifier they never typed in the first place would be a
    worse suggestion than the human-facing name, even when the key
    happens to be a closer character-for-character match.

    The comparison itself is case-folded before scoring -- `difflib`'s
    `SequenceMatcher` compares characters literally, so "cnstrt" vs.
    "CNSTRCT" would otherwise score as barely similar despite being the
    same word -- and the *original*, properly-cased name is what gets
    returned.
    """

    names_by_casefold = {
        record.name.casefold(): record.name for record in repository.list_applications_with_counts()
    }
    matches = difflib.get_close_matches(name_or_key.casefold(), names_by_casefold.keys(), n=1, cutoff=0.6)
    return names_by_casefold[matches[0]] if matches else None


@dataclass(frozen=True, slots=True)
class PortView:
    container_port: int
    protocol: str
    host_binding: Optional[str]  # e.g. "0.0.0.0:3000", or None when unbound


@dataclass(frozen=True, slots=True)
class ContainerDetail:
    name: str
    docker_state: str
    docker_health: Optional[str]
    restart_count: int
    ports: tuple[PortView, ...]


@dataclass(frozen=True, slots=True)
class ServiceDetail:
    compose_service: Optional[str]
    display_name: str
    status: HealthStatus
    container: Optional[ContainerDetail]  # None if it has no observation yet


@dataclass(frozen=True, slots=True)
class ApplicationDetail:
    key: str
    name: str
    status: HealthStatus
    last_seen_at: datetime
    services: tuple[ServiceDetail, ...]
    open_incident: Optional[IncidentWithApplicationRecord]
    #: Milestone 16 -- see `ApplicationSummary`'s own fields.
    host_key: str = LOCAL_HOST_KEY
    host_display_name: str = "Local Host"


def get_application_detail(
    repository: Repository,
    *,
    now: datetime,
    name_or_key: str,
    rules: HealthRules = DEFAULT_HEALTH_RULES,
) -> Optional[ApplicationDetail]:
    """Full current-state detail for one application, or None if
    `name_or_key` matches nothing (the caller decides how to report
    that -- see `suggest_application_name` for the "did you mean" half).

    Ports/docker state/docker health/restart count come from each
    container's *latest* Observation -- already-allowlisted data only
    (Milestone 3's label allowlist); nothing here ever reads raw Docker
    labels or environment variables, because nothing persisted ever
    contains them in the first place.
    """

    key = find_application_key(repository, name_or_key)
    if key is None:
        return None

    record = repository.get_application(key)
    host = repository.get_host_by_id(record.host_id) if record.host_id is not None else None
    host_key = host.host_key if host is not None else LOCAL_HOST_KEY

    collector_status = get_collector_status(repository, now=now, rules=rules)
    # See `_host_freshness_map`'s own docstring -- this application's
    # (and its services') freshness is judged by its *owning host*, not
    # unconditionally by the local collector's own liveness.
    data_is_fresh = _host_freshness_map(
        repository, now=now, local_data_is_fresh=collector_status.data_is_fresh
    ).get(host_key, False)

    app_status = _current_status(repository, scope="application", scope_id=record.id, data_is_fresh=data_is_fresh)

    services: list[ServiceDetail] = []
    for service_record in repository.get_services_for_application(record.id):
        service_status = _current_status(
            repository, scope="service", scope_id=service_record.id, data_is_fresh=data_is_fresh,
        )
        display_name = service_record.compose_service or service_record.name

        container_detail: Optional[ContainerDetail] = None
        containers = repository.get_containers_for_service(service_record.id)
        if containers:
            container_record = containers[0]  # v0.1: exactly one container per service
            observation = repository.get_latest_observation(container_record.container_id)
            if observation is not None:
                ports = tuple(
                    PortView(
                        container_port=port.container_port,
                        protocol=port.protocol.value,
                        host_binding=(
                            f"{port.host_ip}:{port.host_port}" if port.host_port is not None else None
                        ),
                    )
                    for port in observation.ports
                )
                container_detail = ContainerDetail(
                    name=container_record.name,
                    docker_state=observation.docker_state.value,
                    docker_health=(
                        observation.docker_health.value if observation.docker_health is not None else None
                    ),
                    restart_count=observation.restart_count,
                    ports=ports,
                )

        services.append(
            ServiceDetail(
                compose_service=service_record.compose_service,
                display_name=display_name,
                status=service_status,
                container=container_detail,
            )
        )

    open_incident = repository.get_open_incident(failure_signature=f"application:{key}")

    return ApplicationDetail(
        key=record.key,
        name=record.name,
        status=app_status,
        last_seen_at=record.last_seen_at,
        services=tuple(services),
        open_incident=open_incident,
        host_key=host.host_key if host is not None else LOCAL_HOST_KEY,
        host_display_name=host.display_name if host is not None else "Local Host",
    )


# --------------------------------------------------------------------------
# Hosts -- Milestone 16
# --------------------------------------------------------------------------

#: Host ONLINE/STALE/OFFLINE status (see `argus.domain.host
#: .evaluate_host_status`) needs a poll-interval scale to judge
#: "recent" against. This module deliberately cannot import the real
#: configured interval from either `argus.collector.loop`
#: (`argus.api`'s own architecture guard forbids any non-doctor route
#: module from transitively reaching `docker`, which that module
#: imports) or from a remote agent (its actual
#: `ARGUS_AGENT_POLL_INTERVAL` is never persisted centrally -- see the
#: milestone's own "Host Heartbeat" section, which only asks for a
#: threshold "for example"). A single constant matching every
#: component's own documented default (15s) is used for every host
#: uniformly instead -- a deliberate v1 simplification, not a
#: precisely-tracked per-host value.
_HOST_STATUS_POLL_INTERVAL_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class HostView:
    host_key: str
    display_name: str
    kind: str
    status: "HostStatus"
    last_seen_at: datetime
    agent_version: Optional[str]
    application_count: int


@dataclass(frozen=True, slots=True)
class HostDetail:
    summary: HostView
    first_seen_at: datetime
    applications: tuple[ApplicationSummary, ...]


def list_host_views(repository: Repository, *, now: datetime) -> list[HostView]:
    """Every registered host (local and agent), with a live-computed
    ONLINE/STALE/OFFLINE status -- never a stored one; see
    `argus.domain.host.evaluate_host_status`."""

    counts_by_host_key: dict[str, int] = {}
    for record in repository.list_applications_with_counts():
        counts_by_host_key[record.host_key] = counts_by_host_key.get(record.host_key, 0) + 1

    return [
        HostView(
            host_key=host.host_key,
            display_name=host.display_name,
            kind=host.kind,
            status=evaluate_host_status(
                last_seen_at=host.last_seen_at, now=now, poll_interval_seconds=_HOST_STATUS_POLL_INTERVAL_SECONDS
            ),
            last_seen_at=host.last_seen_at,
            agent_version=host.agent_version,
            application_count=counts_by_host_key.get(host.host_key, 0),
        )
        for host in repository.list_hosts()
    ]


def get_host_detail(repository: Repository, *, now: datetime, host_key: str) -> Optional[HostDetail]:
    host = repository.get_host_by_key(host_key)
    if host is None:
        return None

    collector_status = get_collector_status(repository, now=now)
    freshness_by_host = _host_freshness_map(repository, now=now, local_data_is_fresh=collector_status.data_is_fresh)

    applications = [
        ApplicationSummary(
            key=record.key,
            name=record.name,
            status=_current_status(
                repository, scope="application", scope_id=record.id,
                data_is_fresh=freshness_by_host.get(record.host_key, False),
            ),
            service_count=record.service_count,
            container_count=record.container_count,
            last_seen_at=record.last_seen_at,
            host_key=record.host_key,
            host_display_name=record.host_display_name,
        )
        for record in repository.list_applications_with_counts()
        if record.host_key == host_key
    ]

    summary = HostView(
        host_key=host.host_key,
        display_name=host.display_name,
        kind=host.kind,
        status=evaluate_host_status(
            last_seen_at=host.last_seen_at, now=now, poll_interval_seconds=_HOST_STATUS_POLL_INTERVAL_SECONDS
        ),
        last_seen_at=host.last_seen_at,
        agent_version=host.agent_version,
        application_count=len(applications),
    )
    return HostDetail(summary=summary, first_seen_at=host.first_seen_at, applications=tuple(applications))


# --------------------------------------------------------------------------
# Incidents / history -- plain facts, no staleness adjustment needed
# --------------------------------------------------------------------------


def list_incidents(
    repository: Repository, *, open_only: bool = False
) -> Sequence[IncidentWithApplicationRecord]:
    """Newest `opened_at` first (`Repository.list_incidents` already
    orders this way). Incidents are historical events, not "current
    status" -- they carry their own fixed `status`/`worst_status` and
    need no staleness reinterpretation."""

    return repository.list_incidents(open_only=open_only)


def list_history(
    repository: Repository, *, name_or_key: str, since: datetime
) -> Optional[Sequence[TransitionHistoryRow]]:
    """Chronological transitions for one application's whole entity tree
    (application/service/container), or None if `name_or_key` matches no
    application."""

    key = find_application_key(repository, name_or_key)
    if key is None:
        return None
    record = repository.get_application(key)
    return repository.list_transitions_for_application(record.id, since=since)


# --------------------------------------------------------------------------
# Evidence -- Milestone 10
#
# Like incidents/history above, evidence is a historical fact record,
# not a "current status" -- it needs no staleness reinterpretation. This
# layer's only real job is resolving a signal's raw `container_id` into
# a human-facing `source_label` (the owning service's compose_service
# name, or the container's own name for a standalone container) --
# exactly the same "display_name" resolution `get_application_detail`
# already does per service, applied here per evidence signal instead.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceView:
    category: EvidenceCategory
    severity: EvidenceSeverity
    count: int
    first_seen_at: datetime
    last_seen_at: datetime
    sample: str
    source_label: str
    source_type: str
    container_id: str


def _resolve_source_label(repository: Repository, container_id: str) -> str:
    container_record = repository.get_container_by_docker_id(container_id)
    if container_record is None:
        return container_id
    service_record = repository.get_service(container_record.service_id)
    if service_record is not None and service_record.compose_service is not None:
        return service_record.compose_service
    return container_record.name


def _to_evidence_view(repository: Repository, signal: EvidenceRecord) -> EvidenceView:
    return EvidenceView(
        category=signal.category,
        severity=signal.severity,
        count=signal.count,
        first_seen_at=signal.first_seen_at,
        last_seen_at=signal.last_seen_at,
        sample=signal.sample,
        source_label=_resolve_source_label(repository, signal.container_id),
        source_type=signal.source_type,
        container_id=signal.container_id,
    )


def list_evidence_for_application(
    repository: Repository, *, name_or_key: str, since: datetime
) -> Optional[list[EvidenceView]]:
    """Evidence for one application since `since`, newest-last-seen
    first, or None if `name_or_key` matches no application."""

    key = find_application_key(repository, name_or_key)
    if key is None:
        return None
    record = repository.get_application(key)
    signals = repository.list_log_signals_for_application(record.id, since=since)
    return [_to_evidence_view(repository, signal) for signal in signals]


def list_evidence_for_incident(repository: Repository, *, incident_id: int) -> Optional[list[EvidenceView]]:
    """Evidence linked to one incident, chronological, or None if
    `incident_id` matches no incident at all (as opposed to a real
    incident simply having no evidence linked, which is `[]`)."""

    if repository.get_incident_by_id(incident_id) is None:
        return None
    signals = repository.list_evidence_for_incident(incident_id)
    return [_to_evidence_view(repository, signal) for signal in signals]
