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
from typing import Mapping, Sequence

from argus.domain.models import (
    Application,
    Container,
    DockerHealth,
    DockerState,
    HealthStatus,
    Observation,
    PortBinding,
    Service,
)
from argus.store.database import DuplicateIncidentError, DuplicateObservationError, PersistenceError

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
    """

    last_tick_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    last_error: str | None


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
    than one round trip per application."""

    id: int
    key: str
    name: str
    is_standalone: bool
    last_seen_at: datetime
    service_count: int
    container_count: int


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
    the owning compose_service/container name), not a raw `scope_id`."""

    id: int
    scope: str
    label: str
    from_status: HealthStatus | None
    to_status: HealthStatus
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class PersistDiscoveryReport:
    applications_written: int
    services_written: int
    containers_written: int
    observations_written: int


def _row_to_application_record(row: sqlite3.Row) -> ApplicationRecord:
    return ApplicationRecord(
        id=row["id"],
        key=row["key"],
        name=row["name"],
        is_standalone=bool(row["is_standalone"]),
        first_seen_at=_text_to_dt(row["first_seen_at"], field_name="applications.first_seen_at"),
        last_seen_at=_text_to_dt(row["last_seen_at"], field_name="applications.last_seen_at"),
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
    return ApplicationCountsRecord(
        id=row["id"],
        key=row["key"],
        name=row["name"],
        is_standalone=bool(row["is_standalone"]),
        last_seen_at=_text_to_dt(row["last_seen_at"], field_name="applications.last_seen_at"),
        service_count=row["service_count"],
        container_count=row["container_count"],
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
    )


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
        self, *, key: str, name: str, is_standalone: bool, observed_at: datetime
    ) -> int:
        """Insert or refresh an application identity row.

        ``first_seen_at`` is set only on first insert and never moves
        forward afterward. ``last_seen_at`` only ever advances (via a
        ``MAX`` comparison) -- an out-of-order call can't rewind it.
        ``name``/``is_standalone`` are treated as current metadata and
        always updated to the latest supplied value.
        """

        observed_at_text = _dt_to_text(observed_at)
        existing = self._conn.execute(
            "SELECT id FROM applications WHERE key = ?", (key,)
        ).fetchone()

        if existing is None:
            cursor = self._conn.execute(
                "INSERT INTO applications (key, name, is_standalone, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, name, int(is_standalone), observed_at_text, observed_at_text),
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
    ) -> int:
        """Insert or refresh a container identity row.

        Keyed by ``container_id`` (Docker's own identity) -- never by
        ``name``, which Docker reuses across recreation. A recreated
        container (same name, new ``container_id``) always produces a
        *new* row here; the old row and its observation history are
        untouched.

        A ``name`` change for the *same* ``container_id`` updates the
        current display name -- name is current metadata, not part of
        the container's historical identity.
        """

        existing = self._conn.execute(
            "SELECT id FROM containers WHERE container_id = ?", (container_id,)
        ).fetchone()

        if existing is None:
            cursor = self._conn.execute(
                "INSERT INTO containers "
                "(service_id, container_id, name, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (service_id, container_id, name, _dt_to_text(first_seen_at), _dt_to_text(last_seen_at)),
            )
            return cursor.lastrowid

        self._conn.execute(
            "UPDATE containers SET service_id = ?, name = ?, last_seen_at = MAX(last_seen_at, ?) "
            "WHERE id = ?",
            (service_id, name, _dt_to_text(last_seen_at), existing["id"]),
        )
        return existing["id"]

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
    ) -> PersistDiscoveryReport:
        """Persist one complete discovery snapshot in a single transaction.

        Either every identity row and every observation from this
        snapshot is written, or none of it is -- an error partway
        through (including a duplicate observation) rolls the whole
        snapshot back rather than leaving a half-written application
        behind.
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
            "SELECT id, key, name, is_standalone, first_seen_at, last_seen_at "
            "FROM applications WHERE key = ?",
            (key,),
        ).fetchone()
        return _row_to_application_record(row) if row is not None else None

    def list_applications(self) -> tuple[ApplicationRecord, ...]:
        rows = self._conn.execute(
            "SELECT id, key, name, is_standalone, first_seen_at, last_seen_at "
            "FROM applications ORDER BY key"
        ).fetchall()
        return tuple(_row_to_application_record(row) for row in rows)

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
            "SELECT last_tick_at, last_success_at, consecutive_failures, last_error "
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
            "       COUNT(DISTINCT c.id) AS container_count "
            "FROM applications a "
            "LEFT JOIN services s ON s.application_id = a.id "
            "LEFT JOIN containers c ON c.service_id = s.id "
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
        # Wrapped in an outer SELECT so ORDER BY unambiguously refers to the
        # combined result's own column names -- SQLite's compound-SELECT
        # column-name rules for a bare ORDER BY on a UNION ALL are fragile
        # otherwise.
        rows = self._conn.execute(
            "SELECT * FROM ("
            "  SELECT ht.id, ht.scope, ht.from_status, ht.to_status, ht.occurred_at, "
            "         'application' AS label "
            "  FROM health_transitions ht "
            "  JOIN applications a ON a.id = ht.scope_id "
            "  WHERE ht.scope = 'application' AND a.id = ? AND ht.occurred_at >= ? "
            "  UNION ALL "
            "  SELECT ht.id, ht.scope, ht.from_status, ht.to_status, ht.occurred_at, "
            "         COALESCE(s.compose_service, s.name) AS label "
            "  FROM health_transitions ht "
            "  JOIN services s ON s.id = ht.scope_id "
            "  WHERE ht.scope = 'service' AND s.application_id = ? AND ht.occurred_at >= ? "
            "  UNION ALL "
            "  SELECT ht.id, ht.scope, ht.from_status, ht.to_status, ht.occurred_at, "
            "         COALESCE(s.compose_service, c.name) AS label "
            "  FROM health_transitions ht "
            "  JOIN containers c ON c.id = ht.scope_id "
            "  JOIN services s ON s.id = c.service_id "
            "  WHERE ht.scope = 'container' AND s.application_id = ? AND ht.occurred_at >= ? "
            ") ORDER BY occurred_at ASC, id ASC",
            (
                application_id, since_text,
                application_id, since_text,
                application_id, since_text,
            ),
        ).fetchall()
        return tuple(_row_to_transition_history_row(row) for row in rows)
