"""Deterministic domain vocabulary for Argus.

These types answer two separate questions, deliberately kept separate:

* ``Container`` / ``Service`` / ``Application`` — *what is this thing?*
  (long-lived identity)
* ``Observation`` — *what did this thing look like at time T?*
  (a point-in-time snapshot)

Nothing in this module talks to Docker, a database, or a language
model. ``Observation.derived_status`` is a field this milestone only
*defines* — the health evaluator that decides its value belongs to
Milestone 2, not here. Likewise, a running container reporting Docker
health "unhealthy" is a perfectly valid ``Observation`` to construct;
this module has no opinion on what that combination *means*.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional

__all__ = [
    "DockerState",
    "DockerHealth",
    "HealthStatus",
    "Protocol",
    "PortBinding",
    "Container",
    "Observation",
    "Service",
    "Application",
    "EvidenceCategory",
    "EvidenceSeverity",
    "EVIDENCE_SEVERITY_RANK",
    "evidence_severity_rank",
    "EvidenceRecord",
]


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


class DockerState(str, Enum):
    """The container lifecycle state Docker itself reports.

    Deliberately contains no health interpretation — that is exactly
    what the Milestone 2 health evaluator is for.
    """

    RUNNING = "running"
    EXITED = "exited"
    RESTARTING = "restarting"
    PAUSED = "paused"
    DEAD = "dead"
    CREATED = "created"


class DockerHealth(str, Enum):
    """The value of a container's ``HEALTHCHECK`` status, when one exists.

    There is no ``NONE`` member here on purpose. Docker's own API omits
    the ``State.Health`` key entirely when no ``HEALTHCHECK`` is
    configured — it is not reporting a fourth health value, it is
    reporting the *absence* of a health check. Modeling that absence as
    Python's ``None`` (i.e. ``Optional[DockerHealth]`` wherever this
    type is used) is a direct, honest translation of what the source
    API actually says, rather than inventing a sentinel member that
    would make "no healthcheck" look like a status value on equal
    footing with "starting" or "unhealthy".
    """

    STARTING = "starting"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


class HealthStatus(str, Enum):
    """The six deterministic states Argus classifies things into.

    Fixed at exactly six values. Finer distinctions (why something is
    STOPPED, for instance) live in ``Observation.derived_detail``, not
    as additional members here.
    """

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    STOPPED = "STOPPED"
    RESTARTING = "RESTARTING"
    UNKNOWN = "UNKNOWN"


class Protocol(str, Enum):
    """Transport protocol for a published port. No other value is valid."""

    TCP = "tcp"
    UDP = "udp"


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------


def _require_nonempty_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name!s} must be a non-empty string, got {value!r}")
    return value


def _require_utc(value: datetime, field_name: str) -> datetime:
    """Reject anything that is not an explicitly UTC, timezone-aware datetime.

    Naive datetimes are never silently assumed to be local time (or
    UTC) — they fail validation. A timezone-aware datetime in a
    non-UTC offset also fails: Argus stores and compares everything in
    UTC, so a caller must convert before handing a value in.
    """
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime, got {type(value)!r}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware (received a naive datetime)")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{field_name} must be in UTC, got offset {value.utcoffset()}")
    return value


def _require_port_number(value: int, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not (1 <= value <= 65535):
        raise ValueError(f"{field_name} must be an integer in 1..65535, got {value!r}")
    return value


def _as_protocol(value: "Protocol | str") -> Protocol:
    return value if isinstance(value, Protocol) else Protocol(value)


def _as_docker_state(value: "DockerState | str") -> DockerState:
    return value if isinstance(value, DockerState) else DockerState(value)


def _as_docker_health(value: "DockerHealth | str | None") -> Optional[DockerHealth]:
    if value is None:
        return None
    return value if isinstance(value, DockerHealth) else DockerHealth(value)


def _as_health_status(value: "HealthStatus | str") -> HealthStatus:
    return value if isinstance(value, HealthStatus) else HealthStatus(value)


def _frozen_labels(value: Mapping[str, str]) -> Mapping[str, str]:
    for key, val in value.items():
        if not isinstance(key, str) or not isinstance(val, str):
            raise TypeError("labels must be a mapping of str to str")
    return MappingProxyType(dict(value))


# --------------------------------------------------------------------------
# PortBinding
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PortBinding:
    """One published port on a container.

    An unbound container port (declared but not mapped to the host)
    is represented with ``host_port=None`` — not omitted.
    """

    container_port: int
    protocol: Protocol
    host_ip: Optional[str] = None
    host_port: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "protocol", _as_protocol(self.protocol))
        _require_port_number(self.container_port, "container_port")
        if self.host_port is not None:
            _require_port_number(self.host_port, "host_port")

    def to_dict(self) -> dict[str, Any]:
        return {
            "container_port": self.container_port,
            "protocol": self.protocol.value,
            "host_ip": self.host_ip,
            "host_port": self.host_port,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PortBinding":
        return cls(
            container_port=data["container_port"],
            protocol=data["protocol"],
            host_ip=data.get("host_ip"),
            host_port=data.get("host_port"),
        )


# --------------------------------------------------------------------------
# Container — long-lived identity
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Container:
    """*What is this thing?* — identity only, never live health.

    Live/point-in-time information (state, health, restart count,
    ports, ...) belongs on ``Observation``, not here. A ``Container``
    answers "which container is this" the same way at every point in
    its life; an ``Observation`` answers "what did it look like just
    now".
    """

    container_id: str
    name: str
    image: str
    compose_project: Optional[str]
    compose_service: Optional[str]
    first_seen_at: datetime
    last_seen_at: datetime

    def __post_init__(self) -> None:
        _require_nonempty_str(self.container_id, "container_id")
        _require_nonempty_str(self.name, "name")
        _require_nonempty_str(self.image, "image")
        if self.compose_project is not None:
            _require_nonempty_str(self.compose_project, "compose_project")
        if self.compose_service is not None:
            _require_nonempty_str(self.compose_service, "compose_service")
        _require_utc(self.first_seen_at, "first_seen_at")
        _require_utc(self.last_seen_at, "last_seen_at")
        if self.last_seen_at < self.first_seen_at:
            raise ValueError("last_seen_at cannot be before first_seen_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "container_id": self.container_id,
            "name": self.name,
            "image": self.image,
            "compose_project": self.compose_project,
            "compose_service": self.compose_service,
            "first_seen_at": self.first_seen_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Container":
        return cls(
            container_id=data["container_id"],
            name=data["name"],
            image=data["image"],
            compose_project=data.get("compose_project"),
            compose_service=data.get("compose_service"),
            first_seen_at=datetime.fromisoformat(data["first_seen_at"]),
            last_seen_at=datetime.fromisoformat(data["last_seen_at"]),
        )


# --------------------------------------------------------------------------
# Observation — a snapshot at time T
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Observation:
    """*What did this container look like at time T?*

    ``derived_status``/``derived_detail`` are fields this milestone
    only defines the shape of. No rule that computes them exists in
    this module — that is Milestone 2's health evaluator. Any
    combination of ``docker_state`` / ``docker_health`` is a
    structurally valid ``Observation``; whether it is a sensible
    combination is a question this module does not ask.
    """

    container_ref: Container
    observed_at: datetime
    docker_state: DockerState
    docker_health: Optional[DockerHealth]
    restart_count: int
    exit_code: Optional[int]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    ports: tuple[PortBinding, ...]
    labels: Mapping[str, str]
    derived_status: HealthStatus
    derived_detail: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.container_ref, Container):
            raise TypeError("container_ref must be a Container instance")
        _require_utc(self.observed_at, "observed_at")

        object.__setattr__(self, "docker_state", _as_docker_state(self.docker_state))
        object.__setattr__(self, "docker_health", _as_docker_health(self.docker_health))
        object.__setattr__(self, "derived_status", _as_health_status(self.derived_status))

        if not isinstance(self.restart_count, int) or isinstance(self.restart_count, bool):
            raise TypeError("restart_count must be an int")
        if self.restart_count < 0:
            raise ValueError("restart_count cannot be negative")

        if self.exit_code is not None:
            if not isinstance(self.exit_code, int) or isinstance(self.exit_code, bool):
                raise TypeError("exit_code must be an int or None")
            if self.exit_code < 0:
                raise ValueError("exit_code cannot be negative")

        if self.started_at is not None:
            _require_utc(self.started_at, "started_at")
        if self.finished_at is not None:
            _require_utc(self.finished_at, "finished_at")

        ports = tuple(self.ports)
        for port in ports:
            if not isinstance(port, PortBinding):
                raise TypeError("ports must contain PortBinding instances")
        object.__setattr__(self, "ports", ports)

        object.__setattr__(self, "labels", _frozen_labels(self.labels))

    def to_dict(self) -> dict[str, Any]:
        return {
            "container_ref": self.container_ref.to_dict(),
            "observed_at": self.observed_at.isoformat(),
            "docker_state": self.docker_state.value,
            "docker_health": self.docker_health.value if self.docker_health is not None else None,
            "restart_count": self.restart_count,
            "exit_code": self.exit_code,
            "started_at": self.started_at.isoformat() if self.started_at is not None else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at is not None else None,
            "ports": [p.to_dict() for p in self.ports],
            "labels": dict(self.labels),
            "derived_status": self.derived_status.value,
            "derived_detail": self.derived_detail,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Observation":
        return cls(
            container_ref=Container.from_dict(data["container_ref"]),
            observed_at=datetime.fromisoformat(data["observed_at"]),
            docker_state=data["docker_state"],
            docker_health=data.get("docker_health"),
            restart_count=data["restart_count"],
            exit_code=data.get("exit_code"),
            started_at=(
                datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None
            ),
            finished_at=(
                datetime.fromisoformat(data["finished_at"]) if data.get("finished_at") else None
            ),
            ports=tuple(PortBinding.from_dict(p) for p in data.get("ports", [])),
            labels=data.get("labels", {}),
            derived_status=data["derived_status"],
            derived_detail=data.get("derived_detail"),
        )


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Service:
    """One logical compose service within an application.

    ``application_key`` is a plain string identifier — the matching
    ``Application.key`` — rather than a live back-reference to an
    ``Application`` object. An ``Application`` holds its ``Service``
    objects directly, so a ``Service`` holding a reference back to its
    ``Application`` would make the two point at each other, which
    turns equality, hashing, and serialization into a graph problem
    for no real benefit here: nothing in Milestone 1 (or the health
    evaluator that follows it) needs to navigate from a service back
    up to its application object, only to know which application it
    belongs to. A plain key keeps the object graph a tree.

    v0.1 assumes one container per service, but ``containers`` is a
    tuple so replica-aware services are a future extension of this
    same type, not a redesign.
    """

    application_key: str
    compose_service: Optional[str]
    containers: tuple[Container, ...]
    derived_status: HealthStatus

    def __post_init__(self) -> None:
        _require_nonempty_str(self.application_key, "application_key")
        if self.compose_service is not None:
            _require_nonempty_str(self.compose_service, "compose_service")

        containers = tuple(self.containers)
        for container in containers:
            if not isinstance(container, Container):
                raise TypeError("containers must contain Container instances")
        object.__setattr__(self, "containers", containers)

        object.__setattr__(self, "derived_status", _as_health_status(self.derived_status))

    @property
    def name(self) -> str:
        """Human-facing name: the compose service name, or the lone container's name."""
        if self.compose_service is not None:
            return self.compose_service
        if self.containers:
            return self.containers[0].name
        return self.application_key

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_key": self.application_key,
            "compose_service": self.compose_service,
            "containers": [c.to_dict() for c in self.containers],
            "derived_status": self.derived_status.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Service":
        return cls(
            application_key=data["application_key"],
            compose_service=data.get("compose_service"),
            containers=tuple(Container.from_dict(c) for c in data.get("containers", [])),
            derived_status=data["derived_status"],
        )


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Application:
    """A Docker Compose project, or a standalone container promoted to
    a single-service application of its own.

    Distinguishes, e.g.::

        CNSTRCT
        ├── api
        ├── postgres
        ├── redis
        └── calc-engine

    (one ``Application``, four ``Service`` objects, ``is_standalone=False``)

    from four unrelated standalone containers (four separate
    ``Application`` objects, each with one ``Service``,
    ``is_standalone=True``).
    """

    key: str
    name: str
    is_standalone: bool
    services: tuple[Service, ...]
    derived_status: HealthStatus

    def __post_init__(self) -> None:
        _require_nonempty_str(self.key, "key")
        _require_nonempty_str(self.name, "name")
        if not isinstance(self.is_standalone, bool):
            raise TypeError("is_standalone must be a bool")

        services = tuple(self.services)
        if len(services) == 0:
            raise ValueError("an Application must have at least one Service")
        for service in services:
            if not isinstance(service, Service):
                raise TypeError("services must contain Service instances")
            if service.application_key != self.key:
                raise ValueError(
                    f"service.application_key {service.application_key!r} does not match "
                    f"application key {self.key!r}"
                )
        object.__setattr__(self, "services", services)

        object.__setattr__(self, "derived_status", _as_health_status(self.derived_status))

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "is_standalone": self.is_standalone,
            "services": [s.to_dict() for s in self.services],
            "derived_status": self.derived_status.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Application":
        return cls(
            key=data["key"],
            name=data["name"],
            is_standalone=data["is_standalone"],
            services=tuple(Service.from_dict(s) for s in data.get("services", [])),
            derived_status=data["derived_status"],
        )


# --------------------------------------------------------------------------
# Evidence -- Milestone 10
#
# Argus already knows *what* changed (Observation/HealthStatus) and *when*
# (health_transitions/incidents, Milestones 4/6). Evidence answers a third,
# separate question: *what else was going on* around that change --
# structured, bounded, redacted facts pulled from container logs and Docker
# facts, not a free-form blob and not an AI-generated explanation. See
# ``argus/evidence/`` for the deterministic collection pipeline that
# produces these; this module only defines the shape.
# --------------------------------------------------------------------------


class EvidenceCategory(str, Enum):
    """A fixed, deliberately small set of deterministic evidence categories.

    Two categories (``CONTAINER_RESTART``, ``CONTAINER_UNHEALTHY``) are
    derived directly from Docker facts (restart_count deltas, health
    status), never from log text. Every other category is only ever
    produced by matching a container's log lines against
    ``argus.evidence.patterns.DEFAULT_PATTERNS`` -- never inferred, never
    guessed, never assigned by a model.
    """

    DB_CONNECTION_TIMEOUT = "db_connection_timeout"
    AUTHENTICATION_FAILURE = "authentication_failure"
    PORT_CONFLICT = "port_conflict"
    MISSING_ENVIRONMENT_VARIABLE = "missing_environment_variable"
    HTTP_5XX = "http_5xx"
    OOM = "oom"
    DISK_PRESSURE = "disk_pressure"
    NETWORK_FAILURE = "network_failure"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    CONTAINER_RESTART = "container_restart"
    CONTAINER_UNHEALTHY = "container_unhealthy"
    GENERIC_ERROR = "generic_error"


class EvidenceSeverity(str, Enum):
    """Four explicit levels -- never inferred from enum declaration order
    or string sort. See ``EVIDENCE_SEVERITY_RANK`` for the explicit
    ranking, matching the same pattern already used for
    ``HealthStatus`` in ``argus.domain.health`` /
    ``argus.incidents.engine``.
    """

    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


#: Explicit rank, higher is worse. A plain dict keyed by enum member --
#: never enum declaration order, never the member's string value -- same
#: discipline as ``argus.domain.health._APPLICATION_SEVERITY_RANK`` and
#: ``argus.incidents.engine._INCIDENT_SEVERITY_RANK``.
EVIDENCE_SEVERITY_RANK: dict[EvidenceSeverity, int] = {
    EvidenceSeverity.INFO: 1,
    EvidenceSeverity.WARNING: 2,
    EvidenceSeverity.HIGH: 3,
    EvidenceSeverity.CRITICAL: 4,
}


def evidence_severity_rank(severity: "EvidenceSeverity") -> int:
    return EVIDENCE_SEVERITY_RANK[severity]


#: The only valid values for EvidenceRecord.source_type -- a log line
#: read from a container's stdout/stderr stream, or a fact Argus already
#: had on hand from Docker itself (restart_count, health status) without
#: reading any log at all.
EVIDENCE_SOURCE_TYPES = frozenset({"container_log", "docker_fact"})


def _as_evidence_category(value: "EvidenceCategory | str") -> EvidenceCategory:
    return value if isinstance(value, EvidenceCategory) else EvidenceCategory(value)


def _as_evidence_severity(value: "EvidenceSeverity | str") -> EvidenceSeverity:
    return value if isinstance(value, EvidenceSeverity) else EvidenceSeverity(value)


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """One aggregated, structured piece of evidence.

    Deliberately does *not* carry an ``incident_id`` field, even though
    early sketches of this shape suggested one: an evidence record must
    be linkable to an incident *before that incident exists yet* (see
    "Evidence Before Incident Open" in the Milestone 10 spec) and, in
    principle, could turn out to be near more than one incident's
    window. Both are a many-to-many relationship over time, not a
    single foreign key on this row -- so the association lives in its
    own link table (``incident_evidence`` / ``IncidentEvidenceRecord``
    in ``argus.store.repository``), never here.

    ``count``/``first_seen_at``/``last_seen_at`` describe one
    *aggregated* signal (see ``argus.evidence.aggregator``), never a
    single raw line held forever -- ``sample`` is one bounded,
    already-redacted representative example, not a log dump.
    """

    id: Optional[int]
    application_key: str
    container_id: str
    category: EvidenceCategory
    severity: EvidenceSeverity
    first_seen_at: datetime
    last_seen_at: datetime
    count: int
    sample: str
    source_type: str
    source_ref: str

    def __post_init__(self) -> None:
        if self.id is not None and (not isinstance(self.id, int) or isinstance(self.id, bool)):
            raise TypeError("id must be an int or None")
        _require_nonempty_str(self.application_key, "application_key")
        _require_nonempty_str(self.container_id, "container_id")
        object.__setattr__(self, "category", _as_evidence_category(self.category))
        object.__setattr__(self, "severity", _as_evidence_severity(self.severity))
        _require_utc(self.first_seen_at, "first_seen_at")
        _require_utc(self.last_seen_at, "last_seen_at")
        if self.last_seen_at < self.first_seen_at:
            raise ValueError("last_seen_at cannot be before first_seen_at")
        if not isinstance(self.count, int) or isinstance(self.count, bool) or self.count < 1:
            raise ValueError("count must be a positive int")
        _require_nonempty_str(self.sample, "sample")
        if self.source_type not in EVIDENCE_SOURCE_TYPES:
            raise ValueError(
                f"source_type must be one of {sorted(EVIDENCE_SOURCE_TYPES)}, got {self.source_type!r}"
            )
        _require_nonempty_str(self.source_ref, "source_ref")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "application_key": self.application_key,
            "container_id": self.container_id,
            "category": self.category.value,
            "severity": self.severity.value,
            "first_seen_at": self.first_seen_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat(),
            "count": self.count,
            "sample": self.sample,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceRecord":
        return cls(
            id=data.get("id"),
            application_key=data["application_key"],
            container_id=data["container_id"],
            category=data["category"],
            severity=data["severity"],
            first_seen_at=datetime.fromisoformat(data["first_seen_at"]),
            last_seen_at=datetime.fromisoformat(data["last_seen_at"]),
            count=data["count"],
            sample=data["sample"],
            source_type=data["source_type"],
            source_ref=data["source_ref"],
        )
