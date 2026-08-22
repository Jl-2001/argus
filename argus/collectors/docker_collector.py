"""Translate raw Docker inspect payloads into Argus domain objects.

This module owns the boundary the Milestone 2 health evaluator
explicitly deferred to it: mapping Docker's own state strings into
``DockerState``/``DockerHealth`` and deciding what happens when Docker
reports something Argus doesn't recognize. It never re-implements
health *rules* — every ``HealthEvaluation`` here comes from actually
calling ``evaluate_container_health`` / ``evaluate_service_health`` /
``evaluate_application_health`` from ``argus.domain.health``.

Nothing in this module mutates Docker state, and nothing here loops or
retries — a single ``discover()`` call is one full, one-shot pass.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Sequence

from argus.collectors.docker_client import ContainerVanishedError, DockerClient
from argus.domain.health import (
    DEFAULT_HEALTH_RULES,
    HealthEvaluation,
    HealthRules,
    evaluate_application_health,
    evaluate_container_health,
    evaluate_service_health,
)
from argus.domain.models import (
    Application,
    Container,
    DockerHealth,
    DockerState,
    HealthStatus,
    Observation,
    PortBinding,
    Protocol,
    Service,
)

__all__ = [
    "DiscoveryResult",
    "SkippedEntity",
    "DiscoveredContainer",
    "TimestampParseError",
    "UnknownDockerStateError",
    "UnknownDockerHealthError",
    "DEFAULT_ALLOWED_LABEL_KEYS",
    "DEFAULT_ALLOWED_LABEL_PREFIXES",
    "parse_container",
    "discover",
    "discover_now",
]

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Label allowlist
#
# The v0.1 specification suggested a `com.docker.compose.*` prefix as the
# default. Inspecting real, local compose containers while building this
# milestone showed why that is not quite right: Compose itself attaches
# several other labels under that same prefix --
# `com.docker.compose.project.working_dir` and `...project.config_files`
# in particular -- that contain a real absolute host filesystem path
# (e.g. a developer's home directory). A prefix wildcard would leak
# those. So the compose labels Argus actually needs are allowlisted by
# *exact key*, not by prefix; only the `argus.` namespace -- entirely
# under Argus's own control -- is allowlisted by prefix.
# --------------------------------------------------------------------------

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"

DEFAULT_ALLOWED_LABEL_KEYS: frozenset[str] = frozenset(
    {COMPOSE_PROJECT_LABEL, COMPOSE_SERVICE_LABEL}
)
DEFAULT_ALLOWED_LABEL_PREFIXES: tuple[str, ...] = ("argus.",)


def _label_allowed(
    key: str, allowed_keys: frozenset[str], allowed_prefixes: Sequence[str]
) -> bool:
    return key in allowed_keys or any(key.startswith(prefix) for prefix in allowed_prefixes)


def _filter_labels(
    raw_labels: Mapping[str, Any] | None,
    allowed_keys: frozenset[str],
    allowed_prefixes: Sequence[str],
) -> dict[str, str]:
    return {
        key: str(value)
        for key, value in (raw_labels or {}).items()
        if _label_allowed(key, allowed_keys, allowed_prefixes)
    }


# --------------------------------------------------------------------------
# Docker state / health parsing -- the forward-compatibility boundary
# Milestone 2 deferred to here
# --------------------------------------------------------------------------


class UnknownDockerStateError(ValueError):
    """Docker reported a container state Argus's DockerState enum has no member for.

    This is deliberately *not* handled by weakening ``DockerState`` --
    the enum stays a closed, fully-enumerated set. A container hitting
    this is skipped for this discovery pass (logged, recorded in
    ``DiscoveryResult.skipped``) rather than crashing discovery of
    every other container, and rather than being silently mapped onto
    an existing state that might mean something different.
    """


class UnknownDockerHealthError(ValueError):
    """Docker reported a ``State.Health`` object with a status Argus doesn't recognize
    (or an empty/missing ``Status`` inside a present ``Health`` object).

    Handled the same way as an unknown Docker state: skip this
    container, don't guess.
    """


_DOCKER_STATE_MAP: dict[str, DockerState] = {
    "running": DockerState.RUNNING,
    "exited": DockerState.EXITED,
    "restarting": DockerState.RESTARTING,
    "paused": DockerState.PAUSED,
    "dead": DockerState.DEAD,
    "created": DockerState.CREATED,
}

_DOCKER_HEALTH_MAP: dict[str, DockerHealth] = {
    "starting": DockerHealth.STARTING,
    "healthy": DockerHealth.HEALTHY,
    "unhealthy": DockerHealth.UNHEALTHY,
}


def _parse_docker_state(raw_status: Any) -> DockerState:
    try:
        return _DOCKER_STATE_MAP[raw_status]
    except (KeyError, TypeError):
        raise UnknownDockerStateError(f"unrecognized Docker state: {raw_status!r}") from None


def _parse_docker_health(state: Mapping[str, Any]) -> DockerHealth | None:
    health_obj = state.get("Health")
    if health_obj is None:
        # No HEALTHCHECK configured -- an absence, not a status. See
        # argus.domain.models.DockerHealth's docstring for why this is
        # Python's `None` rather than a `NONE` enum member.
        return None
    status = health_obj.get("Status")
    if not status:
        raise UnknownDockerHealthError("State.Health present but Status is missing or empty")
    try:
        return _DOCKER_HEALTH_MAP[status]
    except KeyError:
        raise UnknownDockerHealthError(f"unrecognized Docker health status: {status!r}") from None


# --------------------------------------------------------------------------
# Timestamp parsing
# --------------------------------------------------------------------------


class TimestampParseError(ValueError):
    """A Docker timestamp string could not be parsed.

    Raised rather than silently defaulting to ``None`` -- a genuinely
    malformed (non-sentinel) timestamp is a data problem worth
    surfacing per-container, not a value Argus should guess about.
    """


_ZERO_TIME_SENTINEL = "0001-01-01T00:00:00Z"

# Docker emits RFC3339 with a variable number of fractional-second digits
# (0 to 9 -- real inspect output from this machine showed both 9-digit and
# 8-digit examples, likely from Go's JSON encoder trimming trailing zeros).
_TIMESTAMP_RE = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<frac>\d+))?"
    r"(?P<tz>Z|[+-]\d{2}:\d{2})$"
)


def _parse_docker_timestamp(raw: str | None, *, field_name: str) -> datetime | None:
    """Parse a Docker RFC3339 timestamp into a UTC-aware ``datetime``.

    Docker's zero-value sentinel (``0001-01-01T00:00:00Z``, meaning
    "this never happened") becomes ``None``, not a real datetime.
    Fractional seconds beyond microsecond precision are truncated, never
    rounded -- rounding 999999500ns up to the next second would fabricate
    precision Argus cannot actually store. Anything else that doesn't
    match Docker's timestamp shape raises ``TimestampParseError`` rather
    than being silently treated as ``None`` or as a best-guess datetime.
    """

    if raw is None or raw == "" or raw == _ZERO_TIME_SENTINEL:
        return None

    match = _TIMESTAMP_RE.match(raw)
    if match is None:
        raise TimestampParseError(f"malformed timestamp for {field_name}: {raw!r}")

    base = match.group("base")
    frac = match.group("frac")
    tz = match.group("tz")
    offset = "+00:00" if tz == "Z" else tz

    if frac:
        microseconds = frac[:6].ljust(6, "0")
        normalized = f"{base}.{microseconds}{offset}"
    else:
        normalized = f"{base}{offset}"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise TimestampParseError(f"malformed timestamp for {field_name}: {raw!r}") from exc

    return parsed.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Port parsing
# --------------------------------------------------------------------------


def _parse_ports(raw_ports: Mapping[str, Any] | None, *, container_id: str) -> tuple[PortBinding, ...]:
    bindings: list[PortBinding] = []
    for key, host_bindings in (raw_ports or {}).items():
        try:
            port_str, _, proto = key.partition("/")
            container_port = int(port_str)
            protocol = Protocol(proto or "tcp")

            if not host_bindings:
                # e.g. "5432/tcp": null -- declared but not published to the host.
                bindings.append(
                    PortBinding(
                        container_port=container_port,
                        protocol=protocol,
                        host_ip=None,
                        host_port=None,
                    )
                )
                continue

            for host_binding in host_bindings:
                host_ip = host_binding.get("HostIp") or None
                raw_host_port = host_binding.get("HostPort")
                host_port = int(raw_host_port) if raw_host_port not in (None, "") else None
                bindings.append(
                    PortBinding(
                        container_port=container_port,
                        protocol=protocol,
                        host_ip=host_ip,
                        host_port=host_port,
                    )
                )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            # A single malformed port entry must not cost the rest of this
            # container's information -- isolate it and move on.
            logger.warning(
                "skipping malformed port entry %r for container %s: %s", key, container_id, exc
            )
            continue
    return tuple(bindings)


# --------------------------------------------------------------------------
# Per-container parsing
# --------------------------------------------------------------------------


def parse_container(
    attrs: Mapping[str, Any],
    *,
    observed_at: datetime,
    allowed_label_keys: frozenset[str] = DEFAULT_ALLOWED_LABEL_KEYS,
    allowed_label_prefixes: Sequence[str] = DEFAULT_ALLOWED_LABEL_PREFIXES,
) -> tuple[Container, Observation]:
    """Parse one Docker inspect payload into a ``(Container, Observation)`` pair.

    Only ``Config.Env``, ``Mounts``, and unrelated labels are
    deliberately never read here at all -- there is no "read everything
    then filter" step for those, they are simply never looked at.

    ``first_seen_at`` / ``last_seen_at`` on the returned ``Container``
    are both set to this same ``observed_at`` -- Milestone 3 has no
    database, so Argus cannot yet know when it first observed this
    container across process runs. This is deliberately *not* Docker's
    own ``State.StartedAt``: "when did the container start" and "when
    did Argus first see it" are different questions, and answering the
    second with the first would fabricate a history Argus doesn't
    actually have. Milestone 4's persistence layer owns the real
    ``first_seen_at`` going forward.

    ``Observation.derived_status`` / ``derived_detail`` are set to the
    same placeholder Milestone 2's own tests use
    (``HealthStatus.UNKNOWN`` / ``None``) -- this function never
    evaluates health. The real ``HealthEvaluation`` for this
    observation is produced separately, by actually calling
    ``evaluate_container_health`` (see ``discover()``), and is returned
    in ``DiscoveryResult.evaluations`` rather than forced into this
    frozen snapshot.
    """

    container_id = attrs["Id"]
    if not container_id:
        raise ValueError("container payload is missing Id")

    raw_name = attrs["Name"]
    name = raw_name[1:] if raw_name.startswith("/") else raw_name

    config = attrs["Config"] or {}
    image = config["Image"]
    raw_labels = config.get("Labels") or {}

    compose_project = raw_labels.get(COMPOSE_PROJECT_LABEL) or None
    compose_service = raw_labels.get(COMPOSE_SERVICE_LABEL) or None
    labels = _filter_labels(raw_labels, allowed_label_keys, allowed_label_prefixes)

    state = attrs["State"] or {}
    docker_state = _parse_docker_state(state.get("Status"))
    docker_health = _parse_docker_health(state)
    restart_count = attrs.get("RestartCount", 0)
    exit_code = state.get("ExitCode")
    started_at = _parse_docker_timestamp(state.get("StartedAt"), field_name="State.StartedAt")
    finished_at = _parse_docker_timestamp(state.get("FinishedAt"), field_name="State.FinishedAt")

    network_settings = attrs.get("NetworkSettings") or {}
    ports = _parse_ports(network_settings.get("Ports"), container_id=container_id)

    container = Container(
        container_id=container_id,
        name=name,
        image=image,
        compose_project=compose_project,
        compose_service=compose_service,
        first_seen_at=observed_at,
        last_seen_at=observed_at,
    )

    observation = Observation(
        container_ref=container,
        observed_at=observed_at,
        docker_state=docker_state,
        docker_health=docker_health,
        restart_count=restart_count,
        exit_code=exit_code,
        started_at=started_at,
        finished_at=finished_at,
        ports=ports,
        labels=labels,
        derived_status=HealthStatus.UNKNOWN,
        derived_detail=None,
    )
    return container, observation


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkippedEntity:
    """A container or an entire application excluded from this discovery pass.

    ``scope`` is ``"container"`` for a single unparseable container, or
    ``"application"`` for a whole compose project excluded because it
    violated the v0.1 one-container-per-service assumption (see
    ``_build_compose_application``). Never carries raw Docker payload
    data -- only an identifier and a human-readable reason.
    """

    scope: str
    identifier: str
    reason: str


@dataclass(frozen=True, slots=True)
class DiscoveredContainer:
    """One container's identity, snapshot, and evaluated health together."""

    container: Container
    observation: Observation
    evaluation: HealthEvaluation


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """The full output of one ``discover()`` pass. Holds no raw Docker payloads."""

    applications: tuple[Application, ...]
    observations: tuple[Observation, ...]
    evaluations: Mapping[str, HealthEvaluation]  # keyed by container_id
    skipped: tuple[SkippedEntity, ...]


# --------------------------------------------------------------------------
# Application / service grouping
# --------------------------------------------------------------------------


def _is_compose_managed(container: Container) -> bool:
    # Both labels are required -- a container with only one of the two
    # (incomplete/unexpected Compose metadata) is treated as standalone
    # rather than grouped under a synthetic or partially-known service key.
    return bool(container.compose_project) and bool(container.compose_service)


def _build_compose_application(
    project: str, members: Sequence[tuple[Container, HealthEvaluation]]
) -> Application:
    by_service: dict[str, list[tuple[Container, HealthEvaluation]]] = defaultdict(list)
    for container, evaluation in members:
        by_service[container.compose_service].append((container, evaluation))

    services = []
    service_evaluations = []
    for service_name, entries in sorted(by_service.items()):
        containers = tuple(container for container, _ in entries)
        container_evaluations = [evaluation for _, evaluation in entries]
        # Raises ValueError if more than one container claims this service --
        # the v0.1 cardinality guard already implemented in Milestone 2,
        # reused here rather than re-checked.
        service_eval = evaluate_service_health(container_evaluations=container_evaluations)
        services.append(
            Service(
                application_key=project,
                compose_service=service_name,
                containers=containers,
                derived_status=service_eval.status,
            )
        )
        service_evaluations.append(service_eval)

    app_eval = evaluate_application_health(service_evaluations=service_evaluations)
    return Application(
        key=project,
        name=project,
        is_standalone=False,
        services=tuple(services),
        derived_status=app_eval.status,
    )


def _build_standalone_application(
    container: Container, evaluation: HealthEvaluation
) -> Application:
    key = f"standalone:{container.name}"
    service_eval = evaluate_service_health(container_evaluations=[evaluation])
    service = Service(
        application_key=key,
        compose_service=None,
        containers=(container,),
        derived_status=service_eval.status,
    )
    app_eval = evaluate_application_health(service_evaluations=[service_eval])
    return Application(
        key=key,
        name=container.name,
        is_standalone=True,
        services=(service,),
        derived_status=app_eval.status,
    )


def _build_applications(
    parsed: Sequence[tuple[Container, Observation, HealthEvaluation]],
) -> tuple[list[Application], list[SkippedEntity]]:
    by_project: dict[str, list[tuple[Container, HealthEvaluation]]] = defaultdict(list)
    standalone: list[tuple[Container, HealthEvaluation]] = []

    for container, _observation, evaluation in parsed:
        if _is_compose_managed(container):
            by_project[container.compose_project].append((container, evaluation))
        else:
            standalone.append((container, evaluation))

    applications: list[Application] = []
    skipped: list[SkippedEntity] = []

    for project, members in sorted(by_project.items()):
        try:
            applications.append(_build_compose_application(project, members))
        except ValueError as exc:
            logger.warning("skipping application %r: %s", project, exc)
            skipped.append(
                SkippedEntity(
                    scope="application",
                    identifier=project,
                    reason=f"duplicate compose service in project {project!r}: {exc}",
                )
            )

    for container, evaluation in standalone:
        applications.append(_build_standalone_application(container, evaluation))

    return applications, skipped


# --------------------------------------------------------------------------
# Top-level discovery
# --------------------------------------------------------------------------

# Errors expected from malformed/unexpected *data* -- as opposed to a real
# programming bug -- and therefore safe to catch and isolate per container.
# Deliberately not a bare `except Exception`: anything outside this tuple
# (e.g. a NameError from an actual bug in this module) still propagates and
# fails loudly.
_EXPECTED_PARSE_ERRORS = (KeyError, TypeError, ValueError, AttributeError)


def discover(
    client: DockerClient,
    *,
    observed_at: datetime,
    allowed_label_keys: frozenset[str] = DEFAULT_ALLOWED_LABEL_KEYS,
    allowed_label_prefixes: Sequence[str] = DEFAULT_ALLOWED_LABEL_PREFIXES,
    rules: HealthRules = DEFAULT_HEALTH_RULES,
    history_provider: Optional[Callable[[str], Sequence[Observation]]] = None,
) -> DiscoveryResult:
    """Run one full, one-shot discovery pass.

    This is *not* a polling loop -- it calls ``client.list_containers()``
    once, ``client.inspect_container()`` once per id, and returns.
    Scheduling repeated calls to this function belongs to Milestone 5.

    ``observed_at`` is a required parameter, not read from the clock in
    here -- see ``discover_now()`` for the live convenience entrypoint
    that does read it, once, at the I/O boundary.

    Every container is evaluated with whatever ``history_provider``
    supplies for it -- ``()`` when ``history_provider`` is ``None`` (the
    default), which is exactly Milestone 3's original stateless
    behavior: a single pass with no persisted history has nothing to
    supply, so restart-loop / recent-restart detection is a structural
    no-op in that mode, by construction, not a bug.

    ``history_provider``, when given, is called once per discovered
    container id and must return that container's own prior
    ``Observation``s (chronological order, as
    ``Repository.get_observations_after`` already returns them) to feed
    into ``evaluate_container_health`` as ``prior_observations``. This
    is the seam Milestone 5's collector loop uses to give discovery
    real historical continuity across polls -- see
    ``CollectorLoop.run_once`` in ``argus.collector.loop``, which builds
    a provider backed by the repository. A caller that never supplies
    one (e.g. every existing Milestone 3 test, and any one-shot,
    database-free use of ``discover()``) sees unchanged behavior.
    """

    container_ids = client.list_containers(all=True)

    parsed: list[tuple[Container, Observation, HealthEvaluation]] = []
    skipped: list[SkippedEntity] = []

    for container_id in container_ids:
        try:
            raw = client.inspect_container(container_id)
        except ContainerVanishedError:
            logger.debug("container vanished before inspect: %s", container_id)
            continue

        try:
            container, observation = parse_container(
                raw.attrs,
                observed_at=observed_at,
                allowed_label_keys=allowed_label_keys,
                allowed_label_prefixes=allowed_label_prefixes,
            )
        except _EXPECTED_PARSE_ERRORS as exc:
            logger.warning("skipping malformed container %s: %s", container_id, exc)
            skipped.append(
                SkippedEntity(scope="container", identifier=container_id, reason=str(exc))
            )
            continue

        prior_observations = history_provider(container_id) if history_provider is not None else ()
        evaluation = evaluate_container_health(
            observation=observation, now=observed_at, prior_observations=prior_observations, rules=rules
        )
        parsed.append((container, observation, evaluation))

    applications, application_skips = _build_applications(parsed)
    skipped.extend(application_skips)

    observations = tuple(observation for _, observation, _ in parsed)
    evaluations = MappingProxyType(
        {container.container_id: evaluation for container, _, evaluation in parsed}
    )

    return DiscoveryResult(
        applications=tuple(applications),
        observations=observations,
        evaluations=evaluations,
        skipped=tuple(skipped),
    )


def discover_now(client: DockerClient, **kwargs: Any) -> DiscoveryResult:
    """Convenience live entrypoint: reads the current UTC time once, here,
    at this infrastructure boundary, then delegates to ``discover()``.

    Unlike Milestone 2's pure health evaluator, a clock read is fine at
    this specific I/O boundary -- this function exists only so real
    callers don't have to construct ``datetime.now(timezone.utc)``
    themselves each time.
    """

    return discover(client, observed_at=datetime.now(timezone.utc), **kwargs)
