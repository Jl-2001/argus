"""Typed persistence operations over an already-open Argus database.

Hard rule: nothing in this module decides HEALTHY vs. DEGRADED, opens
an incident, detects a restart loop, or computes an application rollup.
It persists values the domain/health layers already produced, exactly
as supplied. Accordingly, this module never imports
``argus.domain.health`` and never calls ``evaluate_container_health`` /
``evaluate_service_health`` / ``evaluate_application_health`` -- see
``tests/unit/test_repository.py``'s architecture guard.

It also never imports ``argus.collectors`` -- ``persist_discovery``
takes plain sequences of domain objects (``applications``,
``observations``), not a ``DiscoveryResult``, specifically so this
package has no dependency on Docker at all, directly or transitively.
Collectors and store are independent siblings beneath ``argus.domain``;
wiring them together is a later milestone's job.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Optional, Sequence

from argus.domain.host import LOCAL_HOST_KEY
from argus.domain.models import (
    Application,
    Container,
    DockerHealth,
    DockerState,
    EvidenceCategory,
    EvidenceRecord,
    EvidenceSeverity,
    HealthStatus,
    Observation,
    PortBinding,
    Service,
)
from argus.store.database import (
    DuplicateExplanationError,
    DuplicateIncidentError,
    DuplicateObservationError,
    PersistenceError,
)

__all__ = [
    "ApplicationRecord",
    "ServiceRecord",
    "ContainerRecord",
    "CollectorStateRecord",
    "TransitionRecord",
    "IncidentRecord",
    "ApplicationCountsRecord",
    "IncidentWithApplicationRecord",
    "TransitionHistoryRow",
    "TRANSITION_SCOPES",
    "PersistDiscoveryReport",
    "IncidentEvidenceRecord",
    "ObservationRecord",
    "ExplanationRecord",
    "RealtimeEventRecord",
    "HostRecord",
    "Repository",
    "resolve_observation_health",
]

_STANDALONE_SERVICE_KEY = "__standalone__"


def _service_key(compose_service: str | None) -> str:
    return compose_service if compose_service is not None else _STANDALONE_SERVICE_KEY


# --------------------------------------------------------------------------
# Timestamp <-> TEXT
# --------------------------------------------------------------------------


def _dt_to_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _text_to_dt(value: str, *, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PersistenceError(f"malformed stored timestamp for {field_name}: {value!r}") from exc
    if parsed.tzinfo is None:
        raise PersistenceError(f"stored timestamp for {field_name} is unexpectedly naive: {value!r}")
    return parsed.astimezone(timezone.utc)


def _optional_dt_to_text(value: datetime | None) -> str | None:
    return _dt_to_text(value) if value is not None else None


def _optional_text_to_dt(value: str | None, *, field_name: str) -> datetime | None:
    return _text_to_dt(value, field_name=field_name) if value is not None else None


# --------------------------------------------------------------------------
# Ports / labels <-> JSON
# --------------------------------------------------------------------------


def _ports_to_json(ports: Sequence[PortBinding]) -> str:
    return json.dumps([port.to_dict() for port in ports], sort_keys=True)


def _json_to_ports(raw: str) -> tuple[PortBinding, ...]:
    return tuple(PortBinding.from_dict(item) for item in json.loads(raw))


def _labels_to_json(labels: Mapping[str, str]) -> str:
    return json.dumps(dict(labels), sort_keys=True)


def _json_to_labels(raw: str) -> dict[str, str]:
    return json.loads(raw)


# --------------------------------------------------------------------------
# Read DTOs
#
# Application/Service/Container domain objects carry a `derived_status`
# (and Container/Service carry things identity rows alone don't have,
# like image or compose_project). Reconstructing a full, honest domain
# object from identity rows alone would mean fabricating a health
# status this milestone never computed. These small records mirror the
# identity tables exactly instead. Observation reads, below, DO
# reconstruct real `Observation` domain objects -- the observations
# table stores every field one needs.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApplicationRecord:
    id: int
    key: str
    name: str
    is_standalone: bool
    first_seen_at: datetime
    last_seen_at: datetime
    #: Milestone 16. ``None`` only for a row written by a caller that
    #: never supplied one (pre-Milestone-16-style direct
    #: ``upsert_application``/``persist_discovery`` calls, still valid
    #: against the nullable FK) -- every real production write
    #: (``argus.collector.loop``, ``argus.api.routes.agents``) always
    #: resolves and passes a real host id first (see
    #: ``Repository.ensure_local_host``).
    host_id: Optional[int] = None


@dataclass(frozen=True, slots=True)
class HostRecord:
    """One row of ``hosts`` (Milestone 16). ``agent_token_hash`` is
    carried here (this module has no reason to hide it from itself --
    it's needed for the ingestion route's own auth check) but is never
    the kind of thing a caller should serialize into an API response or
    a CLI print statement; see ``argus.api.models``'s host response
    shapes, which deliberately never include it.
    """

    id: int
    host_key: str
    agent_id: str | None
    display_name: str
    kind: str  # "local" | "agent"
    agent_token_hash: str | None
    agent_version: str | None
    first_seen_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class ServiceRecord:
    id: int
    application_id: int
    compose_service: str | None
    name: str
    first_seen_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class ContainerRecord:
    id: int
    service_id: int
    container_id: str
    name: str
    first_seen_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class CollectorStateRecord:
    """The collector's own liveness -- independent of anything it's watching.

    ``None`` fields mean "no tick has ever recorded this yet" (a brand
    new database), not a real timestamp.

    The three ``evidence_*`` fields (schema v4, Milestone 10) are the
    *evidence* subsystem's own, separate liveness -- deliberately not
    folded into ``last_tick_at``/``last_success_at``/
    ``consecutive_failures``/``last_error`` above, which stay about core
    discovery/health monitoring only. A container's log stream being
    briefly unreadable must never look like core monitoring itself
    failed.
    """

    last_tick_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    last_error: str | None
    last_evidence_success_at: datetime | None = None
    consecutive_evidence_failures: int = 0
    last_evidence_error: str | None = None


#: The only valid values for health_transitions.scope / incidents.scope --
#: enforced here (Python) and by a CHECK constraint (schema.sql). v0.1
#: opens incidents at "application" scope only; "container"/"service" are
#: still valid *transition* scopes, just never an incident scope yet.
TRANSITION_SCOPES = frozenset({"container", "service", "application"})


@dataclass(frozen=True, slots=True)
class TransitionRecord:
    """One row of health_transitions -- a single detected status change."""

    id: int
    scope: str
    scope_id: int
    from_status: HealthStatus | None
    to_status: HealthStatus
    occurred_at: datetime
    observation_id: int | None


@dataclass(frozen=True, slots=True)
class IncidentRecord:
    """One row of incidents. ``status`` is the incident's own lifecycle
    state (``"open"`` / ``"resolved"``) -- distinct from ``HealthStatus``,
    which is what ``opening_status``/``worst_status`` hold."""

    id: int
    scope: str
    scope_id: int
    failure_signature: str
    opened_at: datetime
    closed_at: datetime | None
    status: str
    opening_status: HealthStatus
    worst_status: HealthStatus
    opening_transition_id: int
    resolving_transition_id: int | None


@dataclass(frozen=True, slots=True)
class ApplicationCountsRecord:
    """One row of `list_applications_with_counts()` -- identity plus how
    many services/containers currently belong to it, in one query rather
    than one round trip per application.

    ``host_key``/``host_display_name`` (Milestone 16) come along for
    free via the same query's own join onto ``hosts`` -- callers that
    want to show "which machine is this on" (the CLI's `argus apps`,
    the dashboard's Applications page) never need a second round trip
    per application to get it.
    """

    id: int
    key: str
    name: str
    is_standalone: bool
    last_seen_at: datetime
    service_count: int
    container_count: int
    host_key: str = LOCAL_HOST_KEY
    host_display_name: str = "Local Host"


@dataclass(frozen=True, slots=True)
class IncidentWithApplicationRecord:
    """One row of `list_incidents()` -- an `IncidentRecord` with its
    application's key/name already resolved via the join, not a
    separate per-row lookup."""

    id: int
    application_key: str
    application_name: str
    failure_signature: str
    opened_at: datetime
    closed_at: datetime | None
    status: str
    opening_status: HealthStatus
    worst_status: HealthStatus


@dataclass(frozen=True, slots=True)
class TransitionHistoryRow:
    """One row of `list_transitions_for_application()` -- a transition
    with its human-facing label already resolved (``"application"``, or
    the owning compose_service/container name), not a raw `scope_id`.

    ``container_docker_id`` (Milestone 11) is Docker's own container id
    when ``scope == "container"``, and ``None`` for application/service
    scope rows -- added so the evidence assembler can look up
    observations near a container-scope transition without a second
    per-row query. Defaulted so every existing caller (``argus history``)
    that never references it is unaffected.
    """

    id: int
    scope: str
    label: str
    from_status: HealthStatus | None
    to_status: HealthStatus
    occurred_at: datetime
    container_docker_id: str | None = None


@dataclass(frozen=True, slots=True)
class PersistDiscoveryReport:
    applications_written: int
    services_written: int
    containers_written: int
    observations_written: int


@dataclass(frozen=True, slots=True)
class IncidentEvidenceRecord:
    """One row of ``incident_evidence`` -- links one log_signal to one
    incident by time proximity only. ``relation`` is always
    ``"temporal_proximity"`` in v0.2 (enforced by the schema's own CHECK
    constraint) -- see ``argus.evidence.association`` for why this must
    never be read as "caused_by"."""

    id: int
    incident_id: int
    log_signal_id: int
    linked_at: datetime
    relation: str


@dataclass(frozen=True, slots=True)
class ExplanationRecord:
    """One row of ``incident_explanations`` (Milestone 12, extended in
    12.1 with ``provider``) -- a validated, trusted AI explanation,
    already persisted, from *some* provider. Never holds an API key, a
    raw unredacted log, or a system prompt. ``summary``/``root_cause``/
    ``confidence`` are denormalized copies of fields already inside
    ``response_json`` (the full serialized
    ``argus.ai.models.IncidentExplanation``), kept as their own columns
    purely so a human or a future query can see them without parsing
    JSON. This module never imports ``argus.ai`` -- reconstructing the
    real ``IncidentExplanation`` object from ``response_json`` is
    ``argus.ai``'s own job (``IncidentExplanation.from_dict``), not
    this one's. ``provider`` is a plain string here (e.g. ``"anthropic"``/
    ``"gemini"``), not ``argus.ai.providers.AIProviderName`` -- this
    module has no reason to depend on that enum.
    """

    id: int
    incident_id: int
    bundle_fingerprint: str
    provider: str
    model: str
    prompt_version: str
    created_at: datetime
    summary: str
    root_cause: str | None
    confidence: str
    input_tokens: int | None
    output_tokens: int | None
    response_json: str


@dataclass(frozen=True, slots=True)
class RealtimeEventRecord:
    """One row of ``realtime_events`` (Milestone 15). ``payload_json`` is
    already-serialized JSON text this module never parses or validates
    -- see ``argus.realtime.emitter`` for what it actually contains
    (always a small, sanitized set of ids/keys/statuses/timestamps/
    counts, never a raw log sample or secret)."""

    id: int
    event_type: str
    occurred_at: datetime
    payload_json: str
    created_at: datetime


def _row_to_application_record(row: sqlite3.Row) -> ApplicationRecord:
    return ApplicationRecord(
        id=row["id"],
        key=row["key"],
        name=row["name"],
        is_standalone=bool(row["is_standalone"]),
        first_seen_at=_text_to_dt(row["first_seen_at"], field_name="applications.first_seen_at"),
        last_seen_at=_text_to_dt(row["last_seen_at"], field_name="applications.last_seen_at"),
        host_id=row["host_id"],
    )


def _row_to_host_record(row: sqlite3.Row) -> HostRecord:
    return HostRecord(
        id=row["id"],
        host_key=row["host_key"],
        agent_id=row["agent_id"],
        display_name=row["display_name"],
        kind=row["kind"],
        agent_token_hash=row["agent_token_hash"],
        agent_version=row["agent_version"],
        first_seen_at=_text_to_dt(row["first_seen_at"], field_name="hosts.first_seen_at"),
        last_seen_at=_text_to_dt(row["last_seen_at"], field_name="hosts.last_seen_at"),
    )


def _row_to_service_record(row: sqlite3.Row) -> ServiceRecord:
    return ServiceRecord(
        id=row["id"],
        application_id=row["application_id"],
        compose_service=row["compose_service"],
        name=row["name"],
        first_seen_at=_text_to_dt(row["first_seen_at"], field_name="services.first_seen_at"),
        last_seen_at=_text_to_dt(row["last_seen_at"], field_name="services.last_seen_at"),
    )


def _row_to_container_record(row: sqlite3.Row) -> ContainerRecord:
    return ContainerRecord(
        id=row["id"],
        service_id=row["service_id"],
        container_id=row["container_id"],
        name=row["name"],
        first_seen_at=_text_to_dt(row["first_seen_at"], field_name="containers.first_seen_at"),
        last_seen_at=_text_to_dt(row["last_seen_at"], field_name="containers.last_seen_at"),
    )


def _row_to_collector_state_record(row: sqlite3.Row | None) -> CollectorStateRecord:
    if row is None:
        return CollectorStateRecord(
            last_tick_at=None, last_success_at=None, consecutive_failures=0, last_error=None
        )
    return CollectorStateRecord(
        last_tick_at=_optional_text_to_dt(
            row["last_tick_at"], field_name="collector_state.last_tick_at"
        ),
        last_success_at=_optional_text_to_dt(
            row["last_success_at"], field_name="collector_state.last_success_at"
        ),
        consecutive_failures=row["consecutive_failures"],
        last_error=row["last_error"],
        last_evidence_success_at=_optional_text_to_dt(
            row["last_evidence_success_at"], field_name="collector_state.last_evidence_success_at"
        ),
        consecutive_evidence_failures=row["consecutive_evidence_failures"],
        last_evidence_error=row["last_evidence_error"],
    )


def _row_to_transition_record(row: sqlite3.Row) -> TransitionRecord:
    return TransitionRecord(
        id=row["id"],
        scope=row["scope"],
        scope_id=row["scope_id"],
        from_status=HealthStatus(row["from_status"]) if row["from_status"] is not None else None,
        to_status=HealthStatus(row["to_status"]),
        occurred_at=_text_to_dt(row["occurred_at"], field_name="health_transitions.occurred_at"),
        observation_id=row["observation_id"],
    )


def _row_to_incident_record(row: sqlite3.Row) -> IncidentRecord:
    return IncidentRecord(
        id=row["id"],
        scope=row["scope"],
        scope_id=row["scope_id"],
        failure_signature=row["failure_signature"],
        opened_at=_text_to_dt(row["opened_at"], field_name="incidents.opened_at"),
        closed_at=_optional_text_to_dt(row["closed_at"], field_name="incidents.closed_at"),
        status=row["status"],
        opening_status=HealthStatus(row["opening_status"]),
        worst_status=HealthStatus(row["worst_status"]),
        opening_transition_id=row["opening_transition_id"],
        resolving_transition_id=row["resolving_transition_id"],
    )


def _row_to_application_counts_record(row: sqlite3.Row) -> ApplicationCountsRecord:
    row_keys = row.keys()
    return ApplicationCountsRecord(
        id=row["id"],
        key=row["key"],
        name=row["name"],
        is_standalone=bool(row["is_standalone"]),
        last_seen_at=_text_to_dt(row["last_seen_at"], field_name="applications.last_seen_at"),
        service_count=row["service_count"],
        container_count=row["container_count"],
        # LEFT JOIN hosts -- a row whose application has no host_id yet
        # (written by a pre-Milestone-16-style caller with no host
        # concept) falls back to the same local-host labels the
        # dataclass field itself defaults to, rather than surfacing a
        # raw ``None``.
        host_key=row["host_key"] if "host_key" in row_keys and row["host_key"] is not None else LOCAL_HOST_KEY,
        host_display_name=(
            row["host_display_name"]
            if "host_display_name" in row_keys and row["host_display_name"] is not None
            else "Local Host"
        ),
    )


def _row_to_incident_with_application_record(row: sqlite3.Row) -> IncidentWithApplicationRecord:
    return IncidentWithApplicationRecord(
        id=row["id"],
        application_key=row["application_key"],
        application_name=row["application_name"],
        failure_signature=row["failure_signature"],
        opened_at=_text_to_dt(row["opened_at"], field_name="incidents.opened_at"),
        closed_at=_optional_text_to_dt(row["closed_at"], field_name="incidents.closed_at"),
        status=row["status"],
        opening_status=HealthStatus(row["opening_status"]),
        worst_status=HealthStatus(row["worst_status"]),
    )


def _row_to_transition_history_row(row: sqlite3.Row) -> TransitionHistoryRow:
    return TransitionHistoryRow(
        id=row["id"],
        scope=row["scope"],
        label=row["label"],
        from_status=HealthStatus(row["from_status"]) if row["from_status"] is not None else None,
        to_status=HealthStatus(row["to_status"]),
        occurred_at=_text_to_dt(row["occurred_at"], field_name="health_transitions.occurred_at"),
        container_docker_id=row["container_docker_id"] if "container_docker_id" in row.keys() else None,
    )


def _row_to_evidence_record(row: sqlite3.Row) -> EvidenceRecord:
    return EvidenceRecord(
        id=row["id"],
        application_key=row["application_key"],
        container_id=row["docker_container_id"],
        category=EvidenceCategory(row["category"]),
        severity=EvidenceSeverity(row["severity"]),
        first_seen_at=_text_to_dt(row["first_seen_at"], field_name="log_signals.first_seen_at"),
        last_seen_at=_text_to_dt(row["last_seen_at"], field_name="log_signals.last_seen_at"),
        count=row["count"],
        sample=row["sample"],
        source_type=row["source_type"],
        source_ref=row["source_ref"],
    )


def _row_to_incident_evidence_record(row: sqlite3.Row) -> IncidentEvidenceRecord:
    return IncidentEvidenceRecord(
        id=row["id"],
        incident_id=row["incident_id"],
        log_signal_id=row["log_signal_id"],
        linked_at=_text_to_dt(row["linked_at"], field_name="incident_evidence.linked_at"),
        relation=row["relation"],
    )


def _row_to_explanation_record(row: sqlite3.Row) -> ExplanationRecord:
    return ExplanationRecord(
        id=row["id"],
        incident_id=row["incident_id"],
        bundle_fingerprint=row["bundle_fingerprint"],
        provider=row["provider"],
        model=row["model"],
        prompt_version=row["prompt_version"],
        created_at=_text_to_dt(row["created_at"], field_name="incident_explanations.created_at"),
        summary=row["summary"],
        root_cause=row["root_cause"],
        confidence=row["confidence"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        response_json=row["response_json"],
    )


def _row_to_realtime_event_record(row: sqlite3.Row) -> RealtimeEventRecord:
    return RealtimeEventRecord(
        id=row["id"],
        event_type=row["event_type"],
        occurred_at=_text_to_dt(row["occurred_at"], field_name="realtime_events.occurred_at"),
        payload_json=row["payload_json"],
        created_at=_text_to_dt(row["created_at"], field_name="realtime_events.created_at"),
    )


_LOG_SIGNAL_JOIN = """
    SELECT
        ls.id, ls.application_id, ls.container_id, ls.category, ls.severity,
        ls.normalized_signature, ls.first_seen_at, ls.last_seen_at, ls.count,
        ls.sample, ls.source_type, ls.source_ref,
        a.key AS application_key, c.container_id AS docker_container_id
    FROM log_signals ls
    JOIN applications a ON a.id = ls.application_id
    JOIN containers c ON c.id = ls.container_id
"""


_OBSERVATION_JOIN = """
    SELECT
        o.id, o.observed_at, o.docker_state, o.docker_health, o.restart_count,
        o.exit_code, o.started_at, o.finished_at, o.image, o.ports_json,
        o.labels_json, o.derived_status, o.derived_detail,
        c.container_id AS docker_container_id, c.name AS container_name,
        c.first_seen_at AS container_first_seen_at, c.last_seen_at AS container_last_seen_at,
        s.compose_service AS compose_service, a.key AS application_key
    FROM observations o
    JOIN containers c ON c.id = o.container_id
    JOIN services s ON s.id = c.service_id
    JOIN applications a ON a.id = s.application_id
"""


def _row_to_container_ref(row: sqlite3.Row) -> Container:
    compose_service = row["compose_service"]
    # A container's compose_project is only meaningful when it belongs to a
    # real compose service -- a standalone application's key (e.g.
    # "standalone:foo") is not a compose project name.
    compose_project = row["application_key"] if compose_service is not None else None
    return Container(
        container_id=row["docker_container_id"],
        name=row["container_name"],
        image=row["image"],
        compose_project=compose_project,
        compose_service=compose_service,
        first_seen_at=_text_to_dt(
            row["container_first_seen_at"], field_name="containers.first_seen_at"
        ),
        last_seen_at=_text_to_dt(
            row["container_last_seen_at"], field_name="containers.last_seen_at"
        ),
    )


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    """An `Observation` paired with its own database row id.

    `Observation` itself (the domain object -- Milestone 1) deliberately
    carries no row id at all; it's a pure point-in-time value, and every
    existing caller of `get_observation_history`/`get_latest_observation`/
    `get_observations_after` only ever needed the observation's own
    fields, never a citable database identity for it. Milestone 11's
    evidence assembler is the first caller that needs both at once (to
    build a stable ``"observation:<id>"`` citation reference) -- rather
    than retrofit an `id` field onto `Observation` itself (which every
    other constructor call site across the whole codebase would then
    need to supply), the three observation-lookup methods built for the
    assembler return this small pairing instead.
    """

    id: int
    observation: Observation


def _row_to_observation(row: sqlite3.Row) -> Observation:
    return Observation(
        container_ref=_row_to_container_ref(row),
        observed_at=_text_to_dt(row["observed_at"], field_name="observations.observed_at"),
        docker_state=DockerState(row["docker_state"]),
        docker_health=DockerHealth(row["docker_health"]) if row["docker_health"] is not None else None,
        restart_count=row["restart_count"],
        exit_code=row["exit_code"],
        started_at=_optional_text_to_dt(row["started_at"], field_name="observations.started_at"),
        finished_at=_optional_text_to_dt(row["finished_at"], field_name="observations.finished_at"),
        ports=_json_to_ports(row["ports_json"]),
        labels=_json_to_labels(row["labels_json"]),
        derived_status=HealthStatus(row["derived_status"]),
        derived_detail=row["derived_detail"],
    )


def _application_observed_at(application: Application) -> datetime:
    timestamps = [
        container.last_seen_at
        for service in application.services
        for container in service.containers
    ]
    if not timestamps:
        raise PersistenceError(
            f"application {application.key!r} has no containers to derive an observation "
            "timestamp from"
        )
    return max(timestamps)


def _service_observed_at(service: Service) -> datetime:
    timestamps = [container.last_seen_at for container in service.containers]
    if not timestamps:
        raise PersistenceError(
            f"service {service.name!r} in application {service.application_key!r} has no "
            "containers to derive an observation timestamp from"
        )
    return max(timestamps)


def resolve_observation_health(
    observation: Observation, *, status: HealthStatus, detail: str | None
) -> Observation:
    """Return a copy of ``observation`` with its health fields replaced.

    Milestone 3's ``discover()`` always constructs each ``Observation``
    with a placeholder ``derived_status=UNKNOWN`` / ``derived_detail=None``
    -- health hasn't been evaluated yet at the point an ``Observation``
    is built, and it is frozen, so it can't be evaluated-then-mutated in
    place. The real ``HealthEvaluation`` for that same observation is
    produced separately (``DiscoveryResult.evaluations``, keyed by
    container id). This function is the small, mechanical seam that
    bridges the two before persistence: it copies a status/detail that
    was already computed elsewhere into a fresh ``Observation``, it
    does not compute one itself. It takes a plain ``HealthStatus`` and
    ``str | None`` rather than a ``HealthEvaluation`` object
    specifically so this module still never imports
    ``argus.domain.health``.

    Actually wiring ``discover()`` -> evaluate -> this ->
    ``persist_discovery`` into a running, scheduled loop is Milestone
    5's job; this only makes that wiring possible.
    """

    return Observation(
        container_ref=observation.container_ref,
        observed_at=observation.observed_at,
        docker_state=observation.docker_state,
        docker_health=observation.docker_health,
        restart_count=observation.restart_count,
        exit_code=observation.exit_code,
        started_at=observation.started_at,
        finished_at=observation.finished_at,
        ports=observation.ports,
        labels=observation.labels,
        derived_status=status,
        derived_detail=detail,
    )


# --------------------------------------------------------------------------
# Shared transitions-in-a-window SQL (list_transitions_for_application /
# get_transitions_in_window both use this shape; only the container-scope
# branch's ORDER BY/WHERE bound differs). Wrapped in an outer SELECT so
# ORDER BY unambiguously refers to the combined result's own column
# names -- SQLite's compound-SELECT column-name rules for a bare ORDER
# BY on a UNION ALL are fragile otherwise. `container_docker_id` is NULL
# for application/service-scope rows -- only a container-scope
# transition has one.
# --------------------------------------------------------------------------

_TRANSITIONS_QUERY_LOWER_BOUND_ONLY = (
    "SELECT * FROM ("
    "  SELECT ht.id, ht.scope, ht.from_status, ht.to_status, ht.occurred_at, "
    "         'application' AS label, NULL AS container_docker_id "
    "  FROM health_transitions ht "
    "  JOIN applications a ON a.id = ht.scope_id "
    "  WHERE ht.scope = 'application' AND a.id = ? AND ht.occurred_at >= ? "
    "  UNION ALL "
    "  SELECT ht.id, ht.scope, ht.from_status, ht.to_status, ht.occurred_at, "
    "         COALESCE(s.compose_service, s.name) AS label, NULL AS container_docker_id "
    "  FROM health_transitions ht "
    "  JOIN services s ON s.id = ht.scope_id "
    "  WHERE ht.scope = 'service' AND s.application_id = ? AND ht.occurred_at >= ? "
    "  UNION ALL "
    "  SELECT ht.id, ht.scope, ht.from_status, ht.to_status, ht.occurred_at, "
    "         COALESCE(s.compose_service, c.name) AS label, c.container_id AS container_docker_id "
    "  FROM health_transitions ht "
    "  JOIN containers c ON c.id = ht.scope_id "
    "  JOIN services s ON s.id = c.service_id "
    "  WHERE ht.scope = 'container' AND s.application_id = ? AND ht.occurred_at >= ? "
    ") ORDER BY occurred_at ASC, id ASC"
)

_TRANSITIONS_QUERY_BOTH_BOUNDS = (
    "SELECT * FROM ("
    "  SELECT ht.id, ht.scope, ht.from_status, ht.to_status, ht.occurred_at, "
    "         'application' AS label, NULL AS container_docker_id "
    "  FROM health_transitions ht "
    "  JOIN applications a ON a.id = ht.scope_id "
    "  WHERE ht.scope = 'application' AND a.id = ? AND ht.occurred_at >= ? AND ht.occurred_at <= ? "
    "  UNION ALL "
    "  SELECT ht.id, ht.scope, ht.from_status, ht.to_status, ht.occurred_at, "
    "         COALESCE(s.compose_service, s.name) AS label, NULL AS container_docker_id "
    "  FROM health_transitions ht "
    "  JOIN services s ON s.id = ht.scope_id "
    "  WHERE ht.scope = 'service' AND s.application_id = ? AND ht.occurred_at >= ? AND ht.occurred_at <= ? "
    "  UNION ALL "
    "  SELECT ht.id, ht.scope, ht.from_status, ht.to_status, ht.occurred_at, "
    "         COALESCE(s.compose_service, c.name) AS label, c.container_id AS container_docker_id "
    "  FROM health_transitions ht "
    "  JOIN containers c ON c.id = ht.scope_id "
    "  JOIN services s ON s.id = c.service_id "
    "  WHERE ht.scope = 'container' AND s.application_id = ? AND ht.occurred_at >= ? AND ht.occurred_at <= ? "
    ") ORDER BY occurred_at ASC, id ASC"
)


class Repository:
    """Typed reads and writes against an already-open Argus database.

    Takes a connection, not a path -- opening/closing the database is
    ``argus.store.database``'s job.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    @contextmanager
    def transaction(self):
        """A single explicit transaction around several write calls.

        The connection runs in full autocommit mode (see
        ``database.open_database``), so individual ``upsert_*``/
        ``insert_*`` methods commit on their own when called standalone.
        This suspends that for the duration of the ``with`` block,
        making every write inside it one atomic unit: any exception
        rolls the whole thing back and re-raises immediately, nothing
        is caught and continued. ``persist_discovery`` (Milestone 4) and
        the incident engine's per-tick batch (Milestone 6) both use
        this same primitive.
        """

        self._conn.execute("BEGIN")
        try:
            yield self
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # ---------------------------------------------------------------
    # Identity writes
    # ---------------------------------------------------------------

    def upsert_application(
        self, *, key: str, name: str, is_standalone: bool, observed_at: datetime, host_id: Optional[int] = None
    ) -> int:
        """Insert or refresh an application identity row.

        ``first_seen_at`` is set only on first insert and never moves
        forward afterward. ``last_seen_at`` only ever advances (via a
        ``MAX`` comparison) -- an out-of-order call can't rewind it.
        ``name``/``is_standalone`` are treated as current metadata and
        always updated to the latest supplied value.

        ``host_id`` (Milestone 16) is written once, at insert, and never
        updated afterward -- an application's owning host is part of its
        identity, not current metadata; ``key`` itself is already
        host-scoped by the time it reaches here (see
        ``argus.domain.host.scope_application_key``), so a mismatched
        ``host_id`` on an update would only ever indicate a caller bug,
        never a legitimate host migration this method needs to handle.
        """

        observed_at_text = _dt_to_text(observed_at)
        existing = self._conn.execute(
            "SELECT id FROM applications WHERE key = ?", (key,)
        ).fetchone()

        if existing is None:
            cursor = self._conn.execute(
                "INSERT INTO applications (key, name, is_standalone, first_seen_at, last_seen_at, host_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, name, int(is_standalone), observed_at_text, observed_at_text, host_id),
            )
            return cursor.lastrowid

        self._conn.execute(
            "UPDATE applications SET name = ?, is_standalone = ?, "
            "last_seen_at = MAX(last_seen_at, ?) WHERE id = ?",
            (name, int(is_standalone), observed_at_text, existing["id"]),
        )
        return existing["id"]

    def upsert_service(
        self,
        *,
        application_id: int,
        compose_service: str | None,
        name: str,
        observed_at: datetime,
    ) -> int:
        """Insert or refresh a service identity row within an application.

        Keyed by ``(application_id, service_key)`` where ``service_key``
        is ``compose_service`` or a fixed sentinel for a standalone
        application's single service -- see schema.sql for why a plain
        ``UNIQUE(application_id, compose_service)`` isn't sufficient.
        """

        key = _service_key(compose_service)
        observed_at_text = _dt_to_text(observed_at)
        existing = self._conn.execute(
            "SELECT id FROM services WHERE application_id = ? AND service_key = ?",
            (application_id, key),
        ).fetchone()

        if existing is None:
            cursor = self._conn.execute(
                "INSERT INTO services "
                "(application_id, compose_service, service_key, name, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (application_id, compose_service, key, name, observed_at_text, observed_at_text),
            )
            return cursor.lastrowid

        self._conn.execute(
            "UPDATE services SET name = ?, last_seen_at = MAX(last_seen_at, ?) WHERE id = ?",
            (name, observed_at_text, existing["id"]),
        )
        return existing["id"]

    def upsert_container(
        self,
        *,
        service_id: int,
        container_id: str,
        name: str,
        first_seen_at: datetime,
        last_seen_at: datetime,
        host_id: Optional[int] = None,
    ) -> int:
        """Insert or refresh a container identity row.

        Keyed by ``container_id`` (Docker's own identity) -- never by
        ``name``, which Docker reuses across recreation. A recreated
        container (same name, new ``container_id``) always produces a
        *new* row here; the old row and its observation history are
        untouched.

        A ``name`` change for the *same* ``container_id`` updates the
        current display name -- name is current metadata, not part of
        the container's historical identity. ``host_id`` (Milestone 16)
        is written once, at insert, for the same reason it is on
        ``upsert_application`` -- see that method's own docstring.
        """

        existing = self._conn.execute(
            "SELECT id FROM containers WHERE container_id = ?", (container_id,)
        ).fetchone()

        if existing is None:
            cursor = self._conn.execute(
                "INSERT INTO containers "
                "(service_id, container_id, name, first_seen_at, last_seen_at, host_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    service_id, container_id, name,
                    _dt_to_text(first_seen_at), _dt_to_text(last_seen_at), host_id,
                ),
            )
            return cursor.lastrowid

        self._conn.execute(
            "UPDATE containers SET service_id = ?, name = ?, last_seen_at = MAX(last_seen_at, ?) "
            "WHERE id = ?",
            (service_id, name, _dt_to_text(last_seen_at), existing["id"]),
        )
        return existing["id"]

    # ---------------------------------------------------------------
    # Host identity -- Milestone 16
    # ---------------------------------------------------------------

    def ensure_local_host(self, *, display_name: str, now: datetime) -> int:
        """Idempotent: returns the local host's row id, creating it if
        it somehow doesn't exist yet (a fresh/migrated database already
        has one -- see ``argus.store.database._ensure_local_host`` --
        so this is normally just a lookup). ``display_name`` updates the
        existing row's label every time this is called (e.g. a
        ``CollectorLoop`` reading a fresh ``ARGUS_HOST_NAME`` on
        restart) -- this is current metadata, not part of a host's
        identity, matching the same discipline ``upsert_application``
        already applies to ``name``.
        """

        existing = self._conn.execute(
            "SELECT id FROM hosts WHERE host_key = ?", (LOCAL_HOST_KEY,)
        ).fetchone()
        now_text = _dt_to_text(now)
        if existing is not None:
            self._conn.execute("UPDATE hosts SET display_name = ? WHERE id = ?", (display_name, existing["id"]))
            return existing["id"]

        cursor = self._conn.execute(
            "INSERT INTO hosts (host_key, agent_id, display_name, kind, agent_token_hash, agent_version, "
            "first_seen_at, last_seen_at) VALUES (?, NULL, ?, 'local', NULL, NULL, ?, ?)",
            (LOCAL_HOST_KEY, display_name, now_text, now_text),
        )
        return cursor.lastrowid

    def create_agent_host(
        self, *, host_key: str, agent_id: str, display_name: str, token_hash: str, now: datetime
    ) -> int:
        """Administrative registration of a new remote agent host (`argus
        agents add`) -- never called from any request-handling path.

        ``agent_id`` is the credential identity a bearer token is
        actually verified against (see schema.sql's own comment on
        ``hosts.agent_id`` for why it is deliberately a separate column
        from ``host_key``) -- generated by the caller (see
        ``argus.cli.commands.agents``), never derived from ``host_key``
        itself. ``token_hash`` is always an already-hashed credential
        (see ``argus.security.hash_token``); this method never sees, and
        this table never stores, the plaintext token. Raises
        ``PersistenceError`` if ``host_key`` (or ``agent_id``) is
        already registered (deliberately not idempotent, unlike
        ``ensure_local_host`` -- re-running this command for an existing
        host would silently issue a second, different token a caller
        might think is the first one still in effect).
        """

        now_text = _dt_to_text(now)
        try:
            cursor = self._conn.execute(
                "INSERT INTO hosts (host_key, agent_id, display_name, kind, agent_token_hash, agent_version, "
                "first_seen_at, last_seen_at) VALUES (?, ?, ?, 'agent', ?, NULL, ?, ?)",
                (host_key, agent_id, display_name, token_hash, now_text, now_text),
            )
        except sqlite3.IntegrityError as exc:
            raise PersistenceError(f"a host with host_key {host_key!r} is already registered") from exc
        return cursor.lastrowid

    def get_host_by_key(self, host_key: str) -> HostRecord | None:
        row = self._conn.execute(
            "SELECT id, host_key, agent_id, display_name, kind, agent_token_hash, agent_version, "
            "first_seen_at, last_seen_at FROM hosts WHERE host_key = ?",
            (host_key,),
        ).fetchone()
        return _row_to_host_record(row) if row is not None else None

    def get_host_by_id(self, host_id: int) -> HostRecord | None:
        row = self._conn.execute(
            "SELECT id, host_key, agent_id, display_name, kind, agent_token_hash, agent_version, "
            "first_seen_at, last_seen_at FROM hosts WHERE id = ?",
            (host_id,),
        ).fetchone()
        return _row_to_host_record(row) if row is not None else None

    def get_host_by_agent_id(self, agent_id: str) -> HostRecord | None:
        """The one lookup ``argus.api.routes.agents`` uses to authenticate
        an ingest request -- by the request's claimed ``agent_id``, never
        by its (separately, and only afterward, checked) ``host_key``.
        See schema.sql's own comment on ``hosts.agent_id``."""

        row = self._conn.execute(
            "SELECT id, host_key, agent_id, display_name, kind, agent_token_hash, agent_version, "
            "first_seen_at, last_seen_at FROM hosts WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        return _row_to_host_record(row) if row is not None else None

    def list_hosts(self) -> tuple[HostRecord, ...]:
        rows = self._conn.execute(
            "SELECT id, host_key, agent_id, display_name, kind, agent_token_hash, agent_version, "
            "first_seen_at, last_seen_at FROM hosts ORDER BY host_key"
        ).fetchall()
        return tuple(_row_to_host_record(row) for row in rows)

    def record_host_heartbeat(self, *, host_id: int, at: datetime, agent_version: str | None = None) -> None:
        """Every valid tick (local) / authenticated ingest (agent)
        advances ``last_seen_at`` -- the one fact
        ``argus.domain.host.evaluate_host_status`` needs. ``last_seen_at``
        only ever advances (``MAX``), same discipline as every other
        ``last_seen_at`` in this module. ``agent_version`` is only
        overwritten when supplied (an agent reports its own version on
        every ingest; the local host has none)."""

        at_text = _dt_to_text(at)
        if agent_version is not None:
            self._conn.execute(
                "UPDATE hosts SET last_seen_at = MAX(last_seen_at, ?), agent_version = ? WHERE id = ?",
                (at_text, agent_version, host_id),
            )
        else:
            self._conn.execute(
                "UPDATE hosts SET last_seen_at = MAX(last_seen_at, ?) WHERE id = ?", (at_text, host_id)
            )

    # ---------------------------------------------------------------
    # Observation write
    # ---------------------------------------------------------------

    def insert_observation(self, *, container_row_id: int, observation: Observation) -> int:
        """Insert one immutable observation snapshot.

        Observation rows are append-only -- this method never updates
        an existing row. A duplicate ``(container, observed_at)`` pair
        raises ``DuplicateObservationError`` (backed by the schema's
        own UNIQUE constraint) rather than silently returning the
        existing row or overwriting it -- an exact repeat of the same
        logical tick is treated as a caller error worth surfacing, not
        something to quietly absorb.
        """

        values = (
            container_row_id,
            _dt_to_text(observation.observed_at),
            observation.docker_state.value,
            observation.docker_health.value if observation.docker_health is not None else None,
            observation.restart_count,
            observation.exit_code,
            _optional_dt_to_text(observation.started_at),
            _optional_dt_to_text(observation.finished_at),
            observation.container_ref.image,
            _ports_to_json(observation.ports),
            _labels_to_json(observation.labels),
            observation.derived_status.value,
            observation.derived_detail,
        )
        try:
            cursor = self._conn.execute(
                "INSERT INTO observations "
                "(container_id, observed_at, docker_state, docker_health, restart_count, "
                " exit_code, started_at, finished_at, image, ports_json, labels_json, "
                " derived_status, derived_detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateObservationError(
                f"an observation for container row {container_row_id} at "
                f"{observation.observed_at.isoformat()} already exists"
            ) from exc
        return cursor.lastrowid

    # ---------------------------------------------------------------
    # Bulk write
    # ---------------------------------------------------------------

    def persist_discovery(
        self,
        *,
        applications: Sequence[Application],
        observations: Sequence[Observation],
        host_id: Optional[int] = None,
    ) -> PersistDiscoveryReport:
        """Persist one complete discovery snapshot in a single transaction.

        Either every identity row and every observation from this
        snapshot is written, or none of it is -- an error partway
        through (including a duplicate observation) rolls the whole
        snapshot back rather than leaving a half-written application
        behind.

        ``host_id`` (Milestone 16) identifies which monitored machine
        this whole snapshot came from -- every application/container row
        this call writes or refreshes is stamped with it. ``applications``
        (and each of their services) must already carry
        host-*scoped* keys by the time they reach this method (see
        ``argus.domain.host.scope_application_key`` and
        ``argus.ingestion.pipeline``, the one place that scoping is
        applied) -- this method itself has no idea what a host is beyond
        the plain integer id it's given. Defaults to ``None`` (stored as
        SQL ``NULL``, valid against the nullable FK) purely so this
        module's own pre-Milestone-16 test suite -- which was never
        about host behavior -- keeps compiling unchanged; every real
        production caller (``argus.collector.loop``,
        ``argus.api.routes.agents``) always resolves and passes a real
        host id first (see ``Repository.ensure_local_host``).
        """

        observations_by_container_id = {
            observation.container_ref.container_id: observation for observation in observations
        }

        applications_written = 0
        services_written = 0
        containers_written = 0
        observations_written = 0

        with self.transaction():
            for application in applications:
                app_observed_at = _application_observed_at(application)
                application_row_id = self.upsert_application(
                    key=application.key,
                    name=application.name,
                    is_standalone=application.is_standalone,
                    observed_at=app_observed_at,
                    host_id=host_id,
                )
                applications_written += 1

                for service in application.services:
                    service_observed_at = _service_observed_at(service)
                    service_row_id = self.upsert_service(
                        application_id=application_row_id,
                        compose_service=service.compose_service,
                        name=service.name,
                        observed_at=service_observed_at,
                    )
                    services_written += 1

                    for container in service.containers:
                        container_row_id = self.upsert_container(
                            service_id=service_row_id,
                            container_id=container.container_id,
                            name=container.name,
                            first_seen_at=container.first_seen_at,
                            last_seen_at=container.last_seen_at,
                            host_id=host_id,
                        )
                        containers_written += 1

                        observation = observations_by_container_id.get(container.container_id)
                        if observation is not None:
                            self.insert_observation(
                                container_row_id=container_row_id, observation=observation
                            )
                            observations_written += 1

        return PersistDiscoveryReport(
            applications_written=applications_written,
            services_written=services_written,
            containers_written=containers_written,
            observations_written=observations_written,
        )

    # ---------------------------------------------------------------
    # Reads
    # ---------------------------------------------------------------

    def get_application(self, key: str) -> ApplicationRecord | None:
        row = self._conn.execute(
            "SELECT id, key, name, is_standalone, first_seen_at, last_seen_at, host_id "
            "FROM applications WHERE key = ?",
            (key,),
        ).fetchone()
        return _row_to_application_record(row) if row is not None else None

    def get_application_by_id(self, application_id: int) -> ApplicationRecord | None:
        """Look up one application by its own row id -- added for
        Milestone 11's evidence assembler, which starts from an
        incident's `scope_id` (an application row id) rather than a
        human-typed name/key."""

        row = self._conn.execute(
            "SELECT id, key, name, is_standalone, first_seen_at, last_seen_at, host_id "
            "FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()
        return _row_to_application_record(row) if row is not None else None

    def list_applications(self) -> tuple[ApplicationRecord, ...]:
        rows = self._conn.execute(
            "SELECT id, key, name, is_standalone, first_seen_at, last_seen_at, host_id "
            "FROM applications ORDER BY key"
        ).fetchall()
        return tuple(_row_to_application_record(row) for row in rows)

    def get_service(self, service_id: int) -> ServiceRecord | None:
        """Look up one service by its own row id -- a plain primary-key
        read, added for Milestone 10's evidence CLI (which resolves a
        `log_signals.container_id` back to its owning service's
        `compose_service` label for display). Every other existing
        service lookup either goes by `(application_id, compose_service)`
        (`get_service_by_key`) or lists every service for one application
        (`get_services_for_application`); neither fits "I already have a
        container's own `service_id`, resolve it directly"."""

        row = self._conn.execute(
            "SELECT id, application_id, compose_service, name, first_seen_at, last_seen_at "
            "FROM services WHERE id = ?",
            (service_id,),
        ).fetchone()
        return _row_to_service_record(row) if row is not None else None

    def get_services_for_application(self, application_id: int) -> tuple[ServiceRecord, ...]:
        rows = self._conn.execute(
            "SELECT id, application_id, compose_service, name, first_seen_at, last_seen_at "
            "FROM services WHERE application_id = ? ORDER BY id",
            (application_id,),
        ).fetchall()
        return tuple(_row_to_service_record(row) for row in rows)

    def get_containers_for_service(self, service_id: int) -> tuple[ContainerRecord, ...]:
        rows = self._conn.execute(
            "SELECT id, service_id, container_id, name, first_seen_at, last_seen_at "
            "FROM containers WHERE service_id = ? ORDER BY id",
            (service_id,),
        ).fetchall()
        return tuple(_row_to_container_record(row) for row in rows)

    def get_container_by_docker_id(self, container_id: str) -> ContainerRecord | None:
        row = self._conn.execute(
            "SELECT id, service_id, container_id, name, first_seen_at, last_seen_at "
            "FROM containers WHERE container_id = ?",
            (container_id,),
        ).fetchone()
        return _row_to_container_record(row) if row is not None else None

    def get_latest_observation(self, container_id: str) -> Observation | None:
        row = self._conn.execute(
            _OBSERVATION_JOIN + " WHERE c.container_id = ? ORDER BY o.observed_at DESC LIMIT 1",
            (container_id,),
        ).fetchone()
        return _row_to_observation(row) if row is not None else None

    def get_observation_history(self, container_id: str) -> tuple[Observation, ...]:
        rows = self._conn.execute(
            _OBSERVATION_JOIN + " WHERE c.container_id = ? ORDER BY o.observed_at ASC",
            (container_id,),
        ).fetchall()
        return tuple(_row_to_observation(row) for row in rows)

    def get_observations_after(
        self, container_id: str, *, after: datetime | None
    ) -> tuple[Observation, ...]:
        """Observation history for one container, chronological, optionally
        bounded to strictly after a given timestamp.

        Used by the incident engine to find when a status genuinely
        first appeared in already-persisted history, without fetching a
        container's entire lifetime of observations when only a recent
        window is relevant. ``after=None`` returns the full history,
        same as ``get_observation_history``.
        """

        if after is None:
            return self.get_observation_history(container_id)
        rows = self._conn.execute(
            _OBSERVATION_JOIN + " WHERE c.container_id = ? AND o.observed_at > ? ORDER BY o.observed_at ASC",
            (container_id, _dt_to_text(after)),
        ).fetchall()
        return tuple(_row_to_observation(row) for row in rows)

    def get_observation_before(self, container_id: str, *, before: datetime) -> ObservationRecord | None:
        """The single most recent observation strictly before `before`,
        paired with its own row id, or `None` if none exists. Added for
        Milestone 11's observation sampling ("one before a transition")
        -- the mirror image of `get_observations_after`, but bounded to
        exactly one row rather than a whole tail, and carrying the row
        id `get_observations_after`'s plain `Observation` results don't
        (see `ObservationRecord`)."""

        row = self._conn.execute(
            _OBSERVATION_JOIN + " WHERE c.container_id = ? AND o.observed_at < ? "
            "ORDER BY o.observed_at DESC LIMIT 1",
            (container_id, _dt_to_text(before)),
        ).fetchone()
        return ObservationRecord(id=row["id"], observation=_row_to_observation(row)) if row is not None else None

    def get_observation_at(self, container_id: str, *, at: datetime) -> ObservationRecord | None:
        """The observation at exactly `at`, paired with its own row id,
        or `None`. Added for Milestone 11: a health transition's own
        `occurred_at` is, by construction (see the Milestone 6/9
        timestamp-accuracy hardening), the exact `observed_at` of the
        observation that first proved the new status -- this looks that
        observation up directly rather than re-deriving it."""

        row = self._conn.execute(
            _OBSERVATION_JOIN + " WHERE c.container_id = ? AND o.observed_at = ?",
            (container_id, _dt_to_text(at)),
        ).fetchone()
        return ObservationRecord(id=row["id"], observation=_row_to_observation(row)) if row is not None else None

    def get_observation_after(self, container_id: str, *, after: datetime) -> ObservationRecord | None:
        """The single earliest observation strictly after `after`,
        paired with its own row id, or `None`. The singular counterpart
        to `get_observation_before`/`get_observation_at`, added for
        Milestone 11 -- deliberately a separate method from the
        pre-existing, plural `get_observations_after` (Milestone 6),
        which returns a whole tail of plain `Observation` values with no
        row id and has its own established callers (the incident
        engine's history provider) this must not disturb.
        """

        row = self._conn.execute(
            _OBSERVATION_JOIN + " WHERE c.container_id = ? AND o.observed_at > ? "
            "ORDER BY o.observed_at ASC LIMIT 1",
            (container_id, _dt_to_text(after)),
        ).fetchone()
        return ObservationRecord(id=row["id"], observation=_row_to_observation(row)) if row is not None else None

    # ---------------------------------------------------------------
    # Collector heartbeat (schema v2, Milestone 5)
    #
    # These four methods are the entire persistence surface for the
    # collector's own liveness. They record values the collector loop
    # already decided (tick started / tick succeeded / tick failed);
    # they do not decide scheduling, backoff, or retry policy -- that
    # stays in argus.collector.loop.
    # ---------------------------------------------------------------

    def get_collector_state(self) -> CollectorStateRecord:
        row = self._conn.execute(
            "SELECT last_tick_at, last_success_at, consecutive_failures, last_error, "
            "       last_evidence_success_at, consecutive_evidence_failures, last_evidence_error "
            "FROM collector_state WHERE id = 1"
        ).fetchone()
        return _row_to_collector_state_record(row)

    def record_tick_started(self, *, at: datetime) -> None:
        """Mark that a collection attempt began at ``at``.

        Called once at the top of every tick, success or failure --
        ``last_tick_at`` always advances; nothing else on this row is
        touched.
        """

        self._conn.execute(
            "INSERT INTO collector_state (id, last_tick_at) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET last_tick_at = excluded.last_tick_at",
            (_dt_to_text(at),),
        )

    def record_tick_success(self, *, at: datetime) -> None:
        """Mark a fully successful tick: discovery + evaluation + persistence
        all completed. Resets the failure streak and clears the last error."""

        self._conn.execute(
            "INSERT INTO collector_state (id, last_tick_at, last_success_at, "
            " consecutive_failures, last_error) "
            "VALUES (1, ?, ?, 0, NULL) "
            "ON CONFLICT(id) DO UPDATE SET last_success_at = excluded.last_success_at, "
            "consecutive_failures = 0, last_error = NULL",
            (_dt_to_text(at), _dt_to_text(at)),
        )

    def record_tick_failure(self, *, error: str) -> int:
        """Mark a failed tick: increments the failure streak and records
        ``error`` (already sanitized by the caller). ``last_success_at`` is
        left untouched -- a failure never advances it. Returns the new
        ``consecutive_failures`` count."""

        self._conn.execute(
            "INSERT INTO collector_state (id, consecutive_failures, last_error) "
            "VALUES (1, 1, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "consecutive_failures = consecutive_failures + 1, last_error = excluded.last_error",
            (error,),
        )
        return self.get_collector_state().consecutive_failures

    # ---------------------------------------------------------------
    # Evidence-collector heartbeat (schema v4, Milestone 10)
    #
    # Deliberately separate from record_tick_*/get_collector_state's core
    # fields above -- see CollectorStateRecord's own docstring for why.
    # ---------------------------------------------------------------

    def record_evidence_tick_success(self, *, at: datetime) -> None:
        """Mark that evidence collection completed without error this
        tick -- resets the evidence failure streak. Called even when zero
        signals were found: "succeeded and found nothing" and "succeeded
        and found something" are both success."""

        self._conn.execute(
            "INSERT INTO collector_state (id, last_evidence_success_at, "
            " consecutive_evidence_failures, last_evidence_error) "
            "VALUES (1, ?, 0, NULL) "
            "ON CONFLICT(id) DO UPDATE SET last_evidence_success_at = excluded.last_evidence_success_at, "
            "consecutive_evidence_failures = 0, last_evidence_error = NULL",
            (_dt_to_text(at),),
        )

    def record_evidence_tick_failure(self, *, error: str) -> int:
        """Mark that evidence collection/persistence failed this tick.
        ``last_evidence_success_at`` is left untouched. Returns the new
        ``consecutive_evidence_failures`` count."""

        self._conn.execute(
            "INSERT INTO collector_state (id, consecutive_evidence_failures, last_evidence_error) "
            "VALUES (1, 1, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "consecutive_evidence_failures = consecutive_evidence_failures + 1, "
            "last_evidence_error = excluded.last_evidence_error",
            (error,),
        )
        return self.get_collector_state().consecutive_evidence_failures

    def get_service_by_key(
        self, *, application_id: int, compose_service: str | None
    ) -> ServiceRecord | None:
        """Look up one service by its (application, compose_service) identity.

        Uses the same ``service_key`` sentinel as ``upsert_service`` (see
        schema.sql's comment on ``services`` for why a standalone
        application's single service can't be found via a plain
        ``compose_service IS NULL`` lookup reliably).
        """

        key = _service_key(compose_service)
        row = self._conn.execute(
            "SELECT id, application_id, compose_service, name, first_seen_at, last_seen_at "
            "FROM services WHERE application_id = ? AND service_key = ?",
            (application_id, key),
        ).fetchone()
        return _row_to_service_record(row) if row is not None else None

    # ---------------------------------------------------------------
    # Health transitions (schema v3, Milestone 6)
    #
    # These two methods are the entire persistence surface for
    # transition history. They record a scope/from/to/when that the
    # incident engine already decided; they do not compare statuses or
    # decide whether a transition occurred -- that is
    # argus.incidents.engine's job.
    # ---------------------------------------------------------------

    def get_last_transition(self, *, scope: str, scope_id: int) -> TransitionRecord | None:
        """The most recently recorded transition for (scope, scope_id), or
        ``None`` if this entity has never had one recorded -- which is
        exactly how the caller recognizes "first observation ever"."""

        if scope not in TRANSITION_SCOPES:
            raise ValueError(f"invalid transition scope {scope!r}; must be one of {TRANSITION_SCOPES}")
        row = self._conn.execute(
            "SELECT id, scope, scope_id, from_status, to_status, occurred_at, observation_id "
            "FROM health_transitions WHERE scope = ? AND scope_id = ? "
            "ORDER BY occurred_at DESC, id DESC LIMIT 1",
            (scope, scope_id),
        ).fetchone()
        return _row_to_transition_record(row) if row is not None else None

    def insert_transition(
        self,
        *,
        scope: str,
        scope_id: int,
        from_status: HealthStatus | None,
        to_status: HealthStatus,
        occurred_at: datetime,
        observation_id: int | None = None,
    ) -> int:
        """Record one transition. Append-only -- there is no update path,
        matching ``observations``' own immutability."""

        if scope not in TRANSITION_SCOPES:
            raise ValueError(f"invalid transition scope {scope!r}; must be one of {TRANSITION_SCOPES}")
        cursor = self._conn.execute(
            "INSERT INTO health_transitions "
            "(scope, scope_id, from_status, to_status, occurred_at, observation_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                scope,
                scope_id,
                from_status.value if from_status is not None else None,
                to_status.value,
                _dt_to_text(occurred_at),
                observation_id,
            ),
        )
        return cursor.lastrowid

    # ---------------------------------------------------------------
    # Incidents (schema v3, Milestone 6, application scope only)
    #
    # Like the transition methods above, these persist a decision the
    # incident engine already made (open / escalate / resolve); they
    # never compare statuses or decide incident lifecycle themselves.
    # ---------------------------------------------------------------

    def get_open_incident(self, *, failure_signature: str) -> IncidentRecord | None:
        row = self._conn.execute(
            "SELECT id, scope, scope_id, failure_signature, opened_at, closed_at, status, "
            "opening_status, worst_status, opening_transition_id, resolving_transition_id "
            "FROM incidents WHERE failure_signature = ? AND status = 'open'",
            (failure_signature,),
        ).fetchone()
        return _row_to_incident_record(row) if row is not None else None

    def get_incident_by_id(self, incident_id: int) -> IncidentRecord | None:
        row = self._conn.execute(
            "SELECT id, scope, scope_id, failure_signature, opened_at, closed_at, status, "
            "opening_status, worst_status, opening_transition_id, resolving_transition_id "
            "FROM incidents WHERE id = ?",
            (incident_id,),
        ).fetchone()
        return _row_to_incident_record(row) if row is not None else None

    def open_incident(
        self,
        *,
        scope_id: int,
        failure_signature: str,
        opened_at: datetime,
        opening_status: HealthStatus,
        opening_transition_id: int,
    ) -> int:
        """Open a new incident. ``worst_status`` starts equal to
        ``opening_status`` -- the first bad status observed is, so far,
        also the worst one seen.

        The schema's partial unique index on
        ``(failure_signature) WHERE status='open'`` is the DB-level
        backstop against ever having two open incidents for the same
        signature; a violation here means the engine's own dedup check
        was bypassed or raced, so it is surfaced as
        ``DuplicateIncidentError`` rather than silently ignored.
        """

        try:
            cursor = self._conn.execute(
                "INSERT INTO incidents "
                "(scope, scope_id, failure_signature, opened_at, status, "
                " opening_status, worst_status, opening_transition_id) "
                "VALUES ('application', ?, ?, ?, 'open', ?, ?, ?)",
                (
                    scope_id,
                    failure_signature,
                    _dt_to_text(opened_at),
                    opening_status.value,
                    opening_status.value,
                    opening_transition_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateIncidentError(
                f"an open incident for {failure_signature!r} already exists"
            ) from exc
        return cursor.lastrowid

    def update_incident_worst_status(self, *, incident_id: int, worst_status: HealthStatus) -> None:
        self._conn.execute(
            "UPDATE incidents SET worst_status = ? WHERE id = ?",
            (worst_status.value, incident_id),
        )

    def resolve_incident(
        self, *, incident_id: int, closed_at: datetime, resolving_transition_id: int
    ) -> None:
        self._conn.execute(
            "UPDATE incidents SET status = 'resolved', closed_at = ?, resolving_transition_id = ? "
            "WHERE id = ?",
            (_dt_to_text(closed_at), resolving_transition_id, incident_id),
        )

    # ---------------------------------------------------------------
    # Read-model queries (Milestone 7)
    #
    # These exist purely to avoid the CLI either duplicating SQL or
    # doing one query per row for something a single join already
    # answers. They return typed rows, not display strings -- staleness
    # classification and human formatting are argus.cli's job, not this
    # module's.
    # ---------------------------------------------------------------

    def list_applications_with_counts(self) -> tuple[ApplicationCountsRecord, ...]:
        rows = self._conn.execute(
            "SELECT a.id, a.key, a.name, a.is_standalone, a.last_seen_at, "
            "       COUNT(DISTINCT s.id) AS service_count, "
            "       COUNT(DISTINCT c.id) AS container_count, "
            "       h.host_key AS host_key, h.display_name AS host_display_name "
            "FROM applications a "
            "LEFT JOIN services s ON s.application_id = a.id "
            "LEFT JOIN containers c ON c.service_id = s.id "
            "LEFT JOIN hosts h ON h.id = a.host_id "
            "GROUP BY a.id "
            "ORDER BY a.key"
        ).fetchall()
        return tuple(_row_to_application_counts_record(row) for row in rows)

    def list_incidents(self, *, open_only: bool = False) -> tuple[IncidentWithApplicationRecord, ...]:
        query = (
            "SELECT i.id, i.failure_signature, i.opened_at, i.closed_at, i.status, "
            "       i.opening_status, i.worst_status, a.key AS application_key, "
            "       a.name AS application_name "
            "FROM incidents i "
            "JOIN applications a ON a.id = i.scope_id "
        )
        if open_only:
            query += "WHERE i.status = 'open' "
        query += "ORDER BY i.opened_at DESC"
        rows = self._conn.execute(query).fetchall()
        return tuple(_row_to_incident_with_application_record(row) for row in rows)

    def list_transitions_for_application(
        self, application_id: int, *, since: datetime
    ) -> tuple[TransitionHistoryRow, ...]:
        """Every container/service/application transition belonging to one
        application, chronological, with human labels already resolved.

        One `UNION ALL` query across all three scopes rather than a
        per-row lookup -- the one place in the store this milestone
        specifically avoids N+1 for a row count that can genuinely grow
        large (a day's worth of transitions across every entity in an
        application), unlike the small, fixed-size application/service
        lists elsewhere.
        """

        since_text = _dt_to_text(since)
        rows = self._conn.execute(
            _TRANSITIONS_QUERY_LOWER_BOUND_ONLY,
            (application_id, since_text, application_id, since_text, application_id, since_text),
        ).fetchall()
        return tuple(_row_to_transition_history_row(row) for row in rows)

    def get_transitions_in_window(
        self, application_id: int, *, window_start: datetime, window_end: datetime
    ) -> tuple[TransitionHistoryRow, ...]:
        """Same shape as `list_transitions_for_application`, bounded on
        both sides -- the evidence assembler's own bounded incident
        context window (Milestone 11), never "all history since X".
        """

        start_text = _dt_to_text(window_start)
        end_text = _dt_to_text(window_end)
        rows = self._conn.execute(
            _TRANSITIONS_QUERY_BOTH_BOUNDS,
            (
                application_id, start_text, end_text,
                application_id, start_text, end_text,
                application_id, start_text, end_text,
            ),
        ).fetchall()
        return tuple(_row_to_transition_history_row(row) for row in rows)

    # ---------------------------------------------------------------
    # Evidence (schema v4, Milestone 10)
    #
    # Same discipline as every write method above: this module persists
    # decisions ``argus.evidence`` already made (which category/severity
    # a line is, whether a candidate extends an existing signal or starts
    # a new one, which incidents a signal's window overlaps) -- it never
    # classifies a log line, never redacts, never decides a bucket
    # boundary, and never decides which incidents are eligible for
    # (re-)association. That logic lives in ``argus.evidence.collector``
    # / ``argus.evidence.association``, mirroring exactly how
    # ``argus.incidents.engine`` (not this module) owns transition/
    # incident *decisions* above.
    # ---------------------------------------------------------------

    def get_log_cursor(self, container_row_id: int) -> datetime | None:
        """The timestamp of the last log line already read for this
        container, or ``None`` if evidence collection has never read
        this container's logs at all."""

        row = self._conn.execute(
            "SELECT last_log_at FROM log_cursors WHERE container_id = ?", (container_row_id,)
        ).fetchone()
        if row is None:
            return None
        return _text_to_dt(row["last_log_at"], field_name="log_cursors.last_log_at")

    def set_log_cursor(self, container_row_id: int, *, last_log_at: datetime, updated_at: datetime) -> None:
        """Advance (or create) this container's log cursor. Never moves
        it backward -- an out-of-order call cannot rewind how much of
        the log stream is considered already-read."""

        self._conn.execute(
            "INSERT INTO log_cursors (container_id, last_log_at, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(container_id) DO UPDATE SET "
            "last_log_at = MAX(last_log_at, excluded.last_log_at), updated_at = excluded.updated_at",
            (container_row_id, _dt_to_text(last_log_at), _dt_to_text(updated_at)),
        )

    def find_latest_log_signal(
        self, *, container_row_id: int, category: str, normalized_signature: str
    ) -> EvidenceRecord | None:
        """The single most-recently-updated ``log_signals`` row for this
        exact (container, category, signature) key, or ``None`` if none
        exists yet.

        A plain "most recent row for this key" lookup -- the same shape
        as ``get_last_transition`` -- with no opinion on whether it is
        still within its aggregation window; that decision belongs to
        ``argus.evidence.collector``, which is the caller.
        """

        row = self._conn.execute(
            _LOG_SIGNAL_JOIN + " WHERE ls.container_id = ? AND ls.category = ? "
            "AND ls.normalized_signature = ? ORDER BY ls.last_seen_at DESC LIMIT 1",
            (container_row_id, category, normalized_signature),
        ).fetchone()
        return _row_to_evidence_record(row) if row is not None else None

    def insert_log_signal(
        self,
        *,
        application_id: int,
        container_row_id: int,
        category: str,
        severity: str,
        normalized_signature: str,
        first_seen_at: datetime,
        last_seen_at: datetime,
        count: int,
        sample: str,
        source_type: str,
        source_ref: str,
    ) -> int:
        """Insert one brand-new aggregated signal row."""

        cursor = self._conn.execute(
            "INSERT INTO log_signals "
            "(application_id, container_id, category, severity, normalized_signature, "
            " first_seen_at, last_seen_at, count, sample, source_type, source_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                application_id, container_row_id, category, severity, normalized_signature,
                _dt_to_text(first_seen_at), _dt_to_text(last_seen_at), count, sample,
                source_type, source_ref,
            ),
        )
        return cursor.lastrowid

    def extend_log_signal(self, log_signal_id: int, *, last_seen_at: datetime, additional_count: int) -> None:
        """Extend an existing signal: advance ``last_seen_at`` and add
        ``additional_count`` to its running ``count``. ``sample`` is
        deliberately never rewritten -- the first occurrence stays the
        representative example for this signal's whole lifetime."""

        self._conn.execute(
            "UPDATE log_signals SET last_seen_at = ?, count = count + ? WHERE id = ?",
            (_dt_to_text(last_seen_at), additional_count, log_signal_id),
        )

    def get_log_signal(self, log_signal_id: int) -> EvidenceRecord | None:
        row = self._conn.execute(
            _LOG_SIGNAL_JOIN + " WHERE ls.id = ?", (log_signal_id,)
        ).fetchone()
        return _row_to_evidence_record(row) if row is not None else None

    def list_log_signals_for_application(
        self, application_id: int, *, since: datetime
    ) -> tuple[EvidenceRecord, ...]:
        """All signals for one application whose ``last_seen_at`` falls at
        or after ``since``, newest first -- the CLI's own read model
        (``argus evidence <application>``)."""

        rows = self._conn.execute(
            _LOG_SIGNAL_JOIN + " WHERE ls.application_id = ? AND ls.last_seen_at >= ? "
            "ORDER BY ls.last_seen_at DESC",
            (application_id, _dt_to_text(since)),
        ).fetchall()
        return tuple(_row_to_evidence_record(row) for row in rows)

    def list_log_signals_in_window(
        self, application_id: int, *, window_start: datetime, window_end: datetime
    ) -> tuple[EvidenceRecord, ...]:
        """Every signal for one application whose own
        ``[first_seen_at, last_seen_at]`` span overlaps
        ``[window_start, window_end]`` -- the overlap test
        ``argus.evidence.association`` uses to find candidates near an
        incident's window. Overlap, not containment: a signal that began
        before ``window_start`` and is still ongoing past it still
        counts, and vice versa.
        """

        rows = self._conn.execute(
            _LOG_SIGNAL_JOIN + " WHERE ls.application_id = ? "
            "AND ls.first_seen_at <= ? AND ls.last_seen_at >= ? "
            "ORDER BY ls.first_seen_at ASC",
            (application_id, _dt_to_text(window_end), _dt_to_text(window_start)),
        ).fetchall()
        return tuple(_row_to_evidence_record(row) for row in rows)

    def link_incident_evidence(self, *, incident_id: int, log_signal_id: int, linked_at: datetime) -> None:
        """Record that ``log_signal_id`` occurred near ``incident_id``'s
        window. Idempotent: linking an already-linked pair again is a
        silent no-op (backed by the schema's own
        ``UNIQUE(incident_id, log_signal_id)``), not an error -- repeated
        per-tick association scans must be safe to re-run.
        """

        self._conn.execute(
            "INSERT OR IGNORE INTO incident_evidence (incident_id, log_signal_id, linked_at) "
            "VALUES (?, ?, ?)",
            (incident_id, log_signal_id, _dt_to_text(linked_at)),
        )

    def list_evidence_for_incident(self, incident_id: int) -> tuple[EvidenceRecord, ...]:
        rows = self._conn.execute(
            _LOG_SIGNAL_JOIN.replace("FROM log_signals ls", "FROM incident_evidence ie "
                                      "JOIN log_signals ls ON ls.id = ie.log_signal_id")
            + " WHERE ie.incident_id = ? ORDER BY ls.first_seen_at ASC",
            (incident_id,),
        ).fetchall()
        return tuple(_row_to_evidence_record(row) for row in rows)

    def list_incidents_for_association(self, *, grace_cutoff: datetime) -> tuple[IncidentRecord, ...]:
        """Every incident that is either still open, or was resolved at or
        after ``grace_cutoff`` -- i.e. every incident whose evidence
        window (see ``argus.evidence.association``) could still be
        actively growing. A plain, explicit-parameter filter; the
        window-semantics *decision* of what ``grace_cutoff`` should be
        belongs to the caller, not here.
        """

        rows = self._conn.execute(
            "SELECT id, scope, scope_id, failure_signature, opened_at, closed_at, status, "
            "opening_status, worst_status, opening_transition_id, resolving_transition_id "
            "FROM incidents WHERE status = 'open' OR closed_at >= ? "
            "ORDER BY opened_at ASC",
            (_dt_to_text(grace_cutoff),),
        ).fetchall()
        return tuple(_row_to_incident_record(row) for row in rows)

    def delete_expired_log_signals(self, *, before: datetime) -> int:
        """Delete unlinked signals whose ``last_seen_at`` is older than
        ``before`` -- the retention policy's mechanism. A signal linked
        to *any* incident (``incident_evidence``) is never deleted here,
        regardless of age -- an incident's own evidence is retained for
        as long as the incident itself is (indefinitely, matching
        Milestone 6's incident history). Returns the number of rows
        deleted.
        """

        cursor = self._conn.execute(
            "DELETE FROM log_signals WHERE last_seen_at < ? "
            "AND id NOT IN (SELECT log_signal_id FROM incident_evidence)",
            (_dt_to_text(before),),
        )
        return cursor.rowcount

    # ---------------------------------------------------------------
    # Incident explanations (schema v5, Milestone 12; `provider` added
    # in schema v6, Milestone 12.1)
    #
    # Same discipline as every write method above: this module persists
    # an already-validated decision (a trusted `IncidentExplanation`)
    # some other layer already made -- it never calls a model provider,
    # never validates a response, and never imports `argus.ai` at all.
    # That logic lives in `argus.ai.explain`/`argus.ai.validation`,
    # mirroring exactly how `argus.incidents.engine` (not this module)
    # owns transition/incident *decisions*. `provider` is accepted here
    # as a plain string (not `argus.ai.providers.AIProviderName`) for
    # the same reason -- this module has no reason to import that enum.
    # ---------------------------------------------------------------

    def get_cached_explanation(
        self, *, incident_id: int, bundle_fingerprint: str, provider: str, model: str, prompt_version: str
    ) -> ExplanationRecord | None:
        """The cache lookup: an explanation already exists for this
        exact (incident, evidence content, provider, model, prompt
        version) combination, or `None` if a fresh model call is needed.
        A Gemini and an Anthropic explanation for the same incident and
        evidence are two different cache entries -- neither is a hit
        for the other's lookup.
        """

        row = self._conn.execute(
            "SELECT id, incident_id, bundle_fingerprint, provider, model, prompt_version, created_at, "
            "summary, root_cause, confidence, input_tokens, output_tokens, response_json "
            "FROM incident_explanations "
            "WHERE incident_id = ? AND bundle_fingerprint = ? AND provider = ? AND model = ? AND prompt_version = ?",
            (incident_id, bundle_fingerprint, provider, model, prompt_version),
        ).fetchone()
        return _row_to_explanation_record(row) if row is not None else None

    def save_explanation(
        self,
        *,
        incident_id: int,
        bundle_fingerprint: str,
        provider: str,
        model: str,
        prompt_version: str,
        created_at: datetime,
        summary: str,
        root_cause: str | None,
        confidence: str,
        input_tokens: int | None,
        output_tokens: int | None,
        response_json: str,
    ) -> int:
        """Persist one validated explanation. Append-only, like
        `observations`/`health_transitions` -- a genuinely new
        `bundle_fingerprint` (evidence changed), `provider`, or
        `prompt_version` gets its own row; nothing here ever overwrites
        a prior explanation's history. Raises `DuplicateExplanationError`
        if the exact same key already exists (the schema's own unique
        index) -- the cache-lookup-before-call flow in
        `argus.ai.explain` should always prevent this from being
        reached in practice.
        """

        try:
            cursor = self._conn.execute(
                "INSERT INTO incident_explanations "
                "(incident_id, bundle_fingerprint, provider, model, prompt_version, created_at, summary, "
                " root_cause, confidence, input_tokens, output_tokens, response_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    incident_id, bundle_fingerprint, provider, model, prompt_version, _dt_to_text(created_at),
                    summary, root_cause, confidence, input_tokens, output_tokens, response_json,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateExplanationError(
                f"an explanation for incident {incident_id} at fingerprint {bundle_fingerprint!r} "
                f"(provider={provider!r}, model={model!r}, prompt_version={prompt_version!r}) already exists"
            ) from exc
        return cursor.lastrowid

    def list_explanations_for_incident(self, incident_id: int) -> tuple[ExplanationRecord, ...]:
        """Every explanation ever generated for one incident, from any
        provider, oldest first -- an audit trail, not just the
        latest/cached one."""

        rows = self._conn.execute(
            "SELECT id, incident_id, bundle_fingerprint, provider, model, prompt_version, created_at, "
            "summary, root_cause, confidence, input_tokens, output_tokens, response_json "
            "FROM incident_explanations WHERE incident_id = ? ORDER BY created_at ASC, id ASC",
            (incident_id,),
        ).fetchall()
        return tuple(_row_to_explanation_record(row) for row in rows)

    # ---------------------------------------------------------------
    # Realtime events (Milestone 15) -- an auxiliary, replayable "something
    # changed" log GET /api/v1/events streams from. Never a second source
    # of truth: `payload_json` is already-serialized JSON text the caller
    # (argus.realtime.emitter) built from a handful of sanitized fields --
    # this module never inspects or validates its contents, exactly like
    # `response_json` on `incident_explanations` above.
    # ---------------------------------------------------------------

    def insert_realtime_event(
        self, *, event_type: str, occurred_at: datetime, payload_json: str, created_at: datetime
    ) -> int:
        """Appends one event row; `id` (SQLite's own AUTOINCREMENT rowid)
        is the monotonic sequence number SSE replay is built on. Never
        raises anything but a real `sqlite3.Error`/`PersistenceError` --
        callers that must never let this fail a tick (see
        `argus.realtime.emitter`) are responsible for catching that
        themselves; this method makes no policy decision about it."""

        cursor = self._conn.execute(
            "INSERT INTO realtime_events (event_type, occurred_at, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (event_type, _dt_to_text(occurred_at), payload_json, _dt_to_text(created_at)),
        )
        return cursor.lastrowid

    def list_realtime_events_since(self, *, after_id: int, limit: int = 500) -> tuple[RealtimeEventRecord, ...]:
        """Every event with `id > after_id`, ascending -- the SSE
        endpoint's own poll query and its `Last-Event-ID` replay path
        share this one method; `after_id=0` means "everything retained"
        (a fresh connect with no prior id to resume from)."""

        rows = self._conn.execute(
            "SELECT id, event_type, occurred_at, payload_json, created_at "
            "FROM realtime_events WHERE id > ? ORDER BY id ASC LIMIT ?",
            (after_id, limit),
        ).fetchall()
        return tuple(_row_to_realtime_event_record(row) for row in rows)

    def get_realtime_event_id_bounds(self) -> tuple[Optional[int], Optional[int]]:
        """`(earliest_retained_id, latest_id)`, or `(None, None)` if no
        event has ever been recorded. The SSE endpoint uses the earliest
        bound to detect a `Last-Event-ID` older than what retention still
        has -- a genuine gap, not something to silently paper over (see
        `stream.reset`)."""

        row = self._conn.execute("SELECT MIN(id), MAX(id) FROM realtime_events").fetchone()
        return (row[0], row[1])

    def prune_realtime_events(self, *, keep_last: int) -> int:
        """Deletes every event row except the most recent `keep_last` --
        the whole retention policy, in one statement. Safe to call after
        every insert (see `argus.realtime.emitter`): a no-op once the
        table is already at or under `keep_last` rows, since the
        subquery's cutoff then computes to `id <= 0`, matching nothing
        that could ever exist. Returns the number of rows actually
        deleted."""

        cursor = self._conn.execute(
            "DELETE FROM realtime_events WHERE id <= (SELECT COALESCE(MAX(id), 0) - ? FROM realtime_events)",
            (keep_last,),
        )
        return cursor.rowcount
