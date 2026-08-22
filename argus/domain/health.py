"""The deterministic health evaluator.

This module turns a container's raw Docker facts into one of the six
``HealthStatus`` values, and rolls container statuses up to service
and application scope. It is a pure input -> output layer:

* it never imports Docker, a database driver, or an AI client
  (enforced by ``tests/unit/test_health_rules.py``'s architecture
  guard, the same pattern used for ``argus.domain.models``);
* it never reads the wall clock itself — ``now`` is always a
  parameter, never ``datetime.now()`` / ``datetime.utcnow()``
  (also enforced by that guard);
* it never reads history from a store — historical context needed for
  a decision (restart-loop detection) is always passed in explicitly
  as ``prior_observations``, never fetched.

Nothing here mutates the frozen ``Observation`` it is given. Evaluation
produces a new, separate ``HealthEvaluation`` result; whatever later
component persists that result back onto a stored snapshot's
``derived_status`` / ``derived_detail`` fields is not this module's
concern. Correspondingly, this module never *reads*
``observation.derived_status`` / ``observation.derived_detail`` as
input either — consuming them would be circular, since they exist on
``Observation`` only as a place this function's own output may
eventually be recorded by something else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from argus.domain.models import DockerHealth, DockerState, HealthStatus, Observation

__all__ = [
    "HealthRules",
    "DEFAULT_HEALTH_RULES",
    "HealthEvaluation",
    "evaluate_container_health",
    "evaluate_service_health",
    "evaluate_application_health",
]


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HealthRules:
    """The rule parameters from the approved v0.1 specification.

    Field names intentionally match the specification's
    ``settings.yaml`` sketch (``unknown_after``, ``restart_loop_window``,
    ...) so a later settings loader can map keys onto this type
    without renaming anything. This is *not* the full settings system —
    it is just the handful of numbers the health rules need, kept
    independent of any file/config-loading concern.
    """

    unknown_after: int = 60
    restart_loop_window: int = 300
    restart_loop_threshold: int = 3
    degraded_restart_threshold: int = 1

    def __post_init__(self) -> None:
        if self.unknown_after <= 0:
            raise ValueError("unknown_after must be a positive number of seconds")
        if self.restart_loop_window <= 0:
            raise ValueError("restart_loop_window must be a positive number of seconds")
        if self.restart_loop_threshold < 1:
            raise ValueError("restart_loop_threshold must be >= 1")
        if self.degraded_restart_threshold < 1:
            raise ValueError("degraded_restart_threshold must be >= 1")
        if self.degraded_restart_threshold >= self.restart_loop_threshold:
            raise ValueError(
                "degraded_restart_threshold must be strictly less than "
                "restart_loop_threshold, or the 'recent restarts' and "
                "'restart loop' bands would overlap or invert"
            )


DEFAULT_HEALTH_RULES = HealthRules()


# --------------------------------------------------------------------------
# Result type
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HealthEvaluation:
    """The evaluator's output: a status plus an optional diagnostic detail.

    Deliberately not an ``Observation`` — evaluating health should never
    require constructing a replacement snapshot. ``detail`` is the same
    concept as ``Observation.derived_detail`` (e.g. ``"stopped_clean"``,
    ``"restart_loop"``); nothing new is introduced beyond what the
    specification named.
    """

    status: HealthStatus
    detail: Optional[str] = None


# --------------------------------------------------------------------------
# Validation helpers (duplicated in miniature from domain.models rather
# than importing its private helper across module boundaries)
# --------------------------------------------------------------------------


def _require_utc(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime, got {type(value)!r}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware (received a naive datetime)")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{field_name} must be in UTC, got offset {value.utcoffset()}")


# --------------------------------------------------------------------------
# Container-level evaluation
# --------------------------------------------------------------------------


def _is_stale(observed_at: datetime, now: datetime, rules: HealthRules) -> bool:
    """Rule 1. Strictly greater-than, per the approved specification.

    An observation exactly ``unknown_after`` seconds old is not yet
    stale; one microsecond older is.
    """

    age_seconds = (now - observed_at).total_seconds()
    return age_seconds > rules.unknown_after


def _restart_delta_within_window(
    observation: Observation,
    prior_observations: Sequence[Observation],
    rules: HealthRules,
) -> int:
    """How much ``restart_count`` increased inside the trailing window.

    ``restart_count`` is a cumulative counter, so this is never a raw
    count — it is ``current - baseline``, where ``baseline`` is the
    smallest ``restart_count`` seen among this *same container's* prior
    observations whose ``observed_at`` falls in
    ``[observation.observed_at - restart_loop_window, observation.observed_at]``
    (both ends inclusive: a prior observation exactly on the window
    edge counts).

    The window is measured backward from the *observation being
    evaluated*, not from ``now`` — staleness already answers "how
    stale is this observation relative to the current wall clock";
    the restart window instead answers "what had this container been
    doing in the run-up to the moment this snapshot was taken", which
    is a property of the observation's own timeline, not of when it
    happens to be evaluated.

    Only prior observations for the *same* ``container_id`` are
    considered. This is what makes a recreated container (a new
    identity, per Milestone 1's design) start with a clean slate
    automatically — there is no special-case "reset" branch, because
    there is simply no matching history to find.

    If no prior observation falls in the window at all (a brand new
    container, or all history purged), the delta is defined as ``0`` —
    absence of history is treated as absence of evidence of a restart
    loop, not as a loop by default.

    If ``restart_count`` is ever lower than the in-window baseline
    (clock skew, out-of-order delivery, or any other anomaly), the
    delta is clamped to ``0`` rather than going negative — a shrinking
    counter is never treated as "a very large number of restarts".
    """

    window_start = observation.observed_at - timedelta(seconds=rules.restart_loop_window)
    in_window_counts = [
        prior.restart_count
        for prior in prior_observations
        if prior.container_ref.container_id == observation.container_ref.container_id
        and window_start <= prior.observed_at <= observation.observed_at
    ]
    if not in_window_counts:
        return 0
    baseline = min(in_window_counts)
    return max(0, observation.restart_count - baseline)


def _running_health(docker_health: Optional[DockerHealth]) -> tuple[HealthStatus, Optional[str]]:
    """Rule 5's docker_health branch, isolated for clarity."""

    if docker_health is None:
        return HealthStatus.HEALTHY, None
    if docker_health is DockerHealth.HEALTHY:
        return HealthStatus.HEALTHY, None
    if docker_health is DockerHealth.STARTING:
        return HealthStatus.DEGRADED, None
    if docker_health is DockerHealth.UNHEALTHY:
        return HealthStatus.UNHEALTHY, None
    raise AssertionError(f"unhandled DockerHealth member: {docker_health!r}")  # exhaustiveness guard


def evaluate_container_health(
    *,
    observation: Observation,
    now: datetime,
    prior_observations: Sequence[Observation] = (),
    rules: HealthRules = DEFAULT_HEALTH_RULES,
) -> HealthEvaluation:
    """Classify one container's ``Observation`` into a ``HealthEvaluation``.

    Rules are applied in this exact, fixed precedence order — the
    first one that matches wins:

    1. staleness (``now`` vs. ``observation.observed_at``)
    2. RESTARTING — either Docker's own live ``restarting`` state, or
       a historical crash-loop detected from ``prior_observations``
    3. STOPPED (``exited``)
    4. DEAD / CREATED / PAUSED
    5. RUNNING (consults ``docker_health`` and, only when the running
       container would otherwise be HEALTHY, a smaller recent-restart
       downgrade)

    The restart-loop clause of rule 2 is checked *before* rule 3,
    regardless of the container's current ``docker_state`` — an
    ``exited`` container that meets the crash-loop threshold reports
    RESTARTING, not STOPPED, because the loop is the more informative
    fact about what is actually happening to it.

    ``DockerState`` has exactly six known members (Milestone 1), and
    every one of them is handled explicitly above — there is
    deliberately no ``else -> UNKNOWN`` fallback branch here. A future
    Docker state Argus does not yet recognize can only ever originate
    where raw Docker API strings are first parsed into ``DockerState``
    — Milestone 3's discovery layer — and that is where a
    forward-compatibility fallback belongs, not inside a function that
    already assumes a valid, fully-typed ``DockerState`` was handed to
    it.

    Staleness is judged against ``observation.observed_at``, not
    ``observation.container_ref.last_seen_at``. ``last_seen_at`` is
    store/identity bookkeeping about the container in general; the
    question this rule answers is "how old is *this particular
    snapshot* right now", which is a property of the observation being
    evaluated, not of the container's broader identity record. Using
    ``observed_at`` also keeps this function's inputs self-contained —
    it needs nothing beyond the ``Observation`` already being passed.
    """

    _require_utc(now, "now")

    if _is_stale(observation.observed_at, now, rules):
        return HealthEvaluation(HealthStatus.UNKNOWN, detail="stale")

    restart_delta = _restart_delta_within_window(observation, prior_observations, rules)

    if restart_delta >= rules.restart_loop_threshold:
        return HealthEvaluation(HealthStatus.RESTARTING, detail="restart_loop")
    if observation.docker_state is DockerState.RESTARTING:
        return HealthEvaluation(HealthStatus.RESTARTING, detail=None)

    if observation.docker_state is DockerState.EXITED:
        if observation.exit_code is None:
            # Docker reported the container exited but did not tell us how.
            # Guessing "clean" or "error" here would be exactly the silent
            # guess the specification forbids — so this gets its own,
            # honestly-uncertain detail value instead of either one.
            return HealthEvaluation(HealthStatus.STOPPED, detail="stopped_unknown")
        if observation.exit_code == 0:
            return HealthEvaluation(HealthStatus.STOPPED, detail="stopped_clean")
        return HealthEvaluation(HealthStatus.STOPPED, detail="stopped_error")

    if observation.docker_state is DockerState.DEAD:
        return HealthEvaluation(HealthStatus.UNHEALTHY, detail="dead")
    if observation.docker_state is DockerState.CREATED:
        return HealthEvaluation(HealthStatus.UNKNOWN, detail="created")
    if observation.docker_state is DockerState.PAUSED:
        return HealthEvaluation(HealthStatus.UNKNOWN, detail="paused")

    assert observation.docker_state is DockerState.RUNNING  # exhaustive by DockerState's 6 members
    base_status, base_detail = _running_health(observation.docker_health)

    if (
        base_status is HealthStatus.HEALTHY
        and rules.degraded_restart_threshold <= restart_delta < rules.restart_loop_threshold
    ):
        # Only a would-be-HEALTHY result is ever downgraded. A container
        # already DEGRADED (health=starting) or UNHEALTHY (health=unhealthy)
        # stays exactly there — there is no rule that pushes DEGRADED to
        # something worse on account of restarts, and UNHEALTHY is never
        # downgraded into anything "better", per the specification.
        return HealthEvaluation(HealthStatus.DEGRADED, detail="recent_restarts")

    return HealthEvaluation(base_status, base_detail)


# --------------------------------------------------------------------------
# Service-level rollup
# --------------------------------------------------------------------------


def evaluate_service_health(
    *, container_evaluations: Sequence[HealthEvaluation]
) -> HealthEvaluation:
    """Roll container evaluations up to one service.

    v0.1 assumes exactly one container per service. That assumption is
    checked, not silently trusted:

    * zero containers -> ``UNKNOWN`` (``detail="no_containers"``). A
      service with nothing to observe is missing information, not
      necessarily "down" — UNKNOWN says exactly that, consistent with
      how staleness/created/paused already mean "we don't know" at
      container scope.
    * exactly one container -> a direct passthrough of its evaluation,
      as the specification requires for v0.1.
    * more than one container -> raises ``ValueError``. Silently
      picking a worst-of rule here would look like replica support
      without any of the design or testing that real replica-aware
      aggregation needs — surfacing the violated assumption loudly is
      the safer choice, and it is exactly the seam where that future
      behavior gets implemented later (Milestone 22's extension point).
    """

    evaluations = list(container_evaluations)

    if len(evaluations) == 0:
        return HealthEvaluation(HealthStatus.UNKNOWN, detail="no_containers")

    if len(evaluations) > 1:
        raise ValueError(
            f"evaluate_service_health received {len(evaluations)} container "
            "evaluations; v0.1 assumes exactly one container per service. "
            "Replica-aware rollup is not implemented yet."
        )

    only = evaluations[0]
    return HealthEvaluation(status=only.status, detail=only.detail)


# --------------------------------------------------------------------------
# Application-level rollup
# --------------------------------------------------------------------------

# Explicit severity ranking used for the worst-of step. A plain dict keyed
# by enum member — never enum declaration order, never the member's string
# value — so nothing here can accidentally depend on how HealthStatus
# happens to be declared or spelled.
_APPLICATION_SEVERITY_RANK: dict[HealthStatus, int] = {
    HealthStatus.UNHEALTHY: 5,
    HealthStatus.STOPPED: 5,  # a *partial* stop ranks exactly as severely as UNHEALTHY
    HealthStatus.RESTARTING: 4,
    HealthStatus.DEGRADED: 3,
    HealthStatus.UNKNOWN: 2,
    HealthStatus.HEALTHY: 1,
}


def evaluate_application_health(
    *, service_evaluations: Sequence[HealthEvaluation]
) -> HealthEvaluation:
    """Roll service evaluations up to one application, in two explicit steps.

    Step 1 — whole-application stop: if *every* service is STOPPED, the
    application is STOPPED. This is the "intentionally, entirely down"
    case.

    Step 2 — otherwise, worst-of by explicit rank. A partial stop (one
    service STOPPED while others are not) ranks exactly as severely as
    UNHEALTHY and is *reported* as UNHEALTHY — STOPPED at application
    scope is reserved for step 1 alone, never produced by step 2, so a
    service that should be running but isn't always reads as the
    application being unhealthy, not as it being "down".

    Zero service evaluations -> ``UNKNOWN`` (``detail="no_services"``),
    for the same "missing information, not a verdict" reason zero
    containers resolves to UNKNOWN in ``evaluate_service_health``. In
    practice ``Application`` itself already forbids zero services at
    construction (Milestone 1), so this only matters for a caller that
    hands this pure function a list directly.
    """

    evaluations = list(service_evaluations)

    if not evaluations:
        return HealthEvaluation(HealthStatus.UNKNOWN, detail="no_services")

    if all(e.status is HealthStatus.STOPPED for e in evaluations):
        return HealthEvaluation(HealthStatus.STOPPED, detail=None)

    worst_rank = max(_APPLICATION_SEVERITY_RANK[e.status] for e in evaluations)
    worst = [e for e in evaluations if _APPLICATION_SEVERITY_RANK[e.status] == worst_rank]

    if worst_rank == _APPLICATION_SEVERITY_RANK[HealthStatus.UNHEALTHY]:
        is_partial_stop = any(e.status is HealthStatus.STOPPED for e in worst)
        return HealthEvaluation(
            HealthStatus.UNHEALTHY, detail="partial_stop" if is_partial_stop else None
        )

    # Every remaining rank maps to exactly one HealthStatus, so any member
    # of `worst` carries the right one.
    return HealthEvaluation(worst[0].status, detail=None)
