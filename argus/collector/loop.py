"""The long-running collector loop.

Wires together three already-complete layers on a timer:

    argus.collectors.docker_collector.discover()   -- Milestone 3
    (the evaluations discover() already computed)  -- Milestone 2, via M3
    argus.store.repository.Repository.persist_discovery()  -- Milestone 4

This module contains no discovery logic, no health rules, and no
persistence mechanics of its own -- only scheduling, failure
classification, and backoff. ``run_once()`` is one deterministic
collection attempt; ``run_forever()`` is the scheduling wrapper around
it. Tests target ``run_once()`` almost exclusively, with a clock and a
sleep function both injected so nothing here depends on real wall-clock
time.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Sequence

from argus.collectors.docker_client import DockerClient, DockerUnavailableError
from argus.collectors.docker_collector import discover
from argus.domain.health import DEFAULT_HEALTH_RULES, HealthRules
from argus.domain.models import Observation
from argus.incidents.engine import IncidentProcessingError, process_transitions_and_incidents
from argus.store.database import PersistenceError
from argus.store.repository import Repository, resolve_observation_health

__all__ = [
    "CollectorConfig",
    "DEFAULT_COLLECTOR_CONFIG",
    "TickResult",
    "MissingEvaluationError",
    "compute_backoff",
    "CollectorLoop",
]

logger = logging.getLogger(__name__)

_MAX_ERROR_LENGTH = 500


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    """The handful of scheduling numbers the collector loop needs.

    Not a general settings system -- just enough to run and back off
    deterministically. A full ``settings.yaml`` loader is a later
    concern; these fields are named so one could map onto it directly
    when it exists.
    """

    poll_interval: float = 15.0
    backoff_initial: float = 15.0
    backoff_max: float = 60.0

    def __post_init__(self) -> None:
        if self.poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if self.backoff_initial <= 0:
            raise ValueError("backoff_initial must be positive")
        if self.backoff_max < self.backoff_initial:
            raise ValueError("backoff_max must be >= backoff_initial")


DEFAULT_COLLECTOR_CONFIG = CollectorConfig()


def compute_backoff(consecutive_failures: int, config: CollectorConfig = DEFAULT_COLLECTOR_CONFIG) -> float:
    """Exponential backoff, capped, deterministic -- no jitter.

    ``consecutive_failures`` 1 -> ``backoff_initial``, doubling each
    time, capped at ``backoff_max``. ``0`` (no failures) -> ``0.0``,
    since a healthy loop doesn't back off at all; it just waits the
    normal ``poll_interval``.
    """

    if consecutive_failures <= 0:
        return 0.0
    return min(config.backoff_initial * (2 ** (consecutive_failures - 1)), config.backoff_max)


# --------------------------------------------------------------------------
# Failure categories
# --------------------------------------------------------------------------


class MissingEvaluationError(RuntimeError):
    """A discovered Observation has no matching HealthEvaluation.

    This is a structural inconsistency in ``DiscoveryResult`` itself --
    a broken contract between its own ``observations`` and
    ``evaluations`` mappings -- not a per-container Docker-data
    problem. A single malformed or vanished container is already
    isolated by Milestone 3 into ``DiscoveryResult.skipped`` and does
    *not* raise this; this is reserved for the case where discovery's
    own output can no longer be trusted, so the tick fails rather than
    persisting an observation with a fabricated placeholder status.
    """


def _sanitize_error(exc: BaseException) -> str:
    """A short, storable summary -- never a full traceback, never raw
    Docker/label/env content (nothing in the failure paths below ever
    passes those into an exception message in the first place)."""

    message = f"{type(exc).__name__}: {exc}"
    if len(message) > _MAX_ERROR_LENGTH:
        message = message[: _MAX_ERROR_LENGTH - len("...(truncated)")] + "...(truncated)"
    return message


# --------------------------------------------------------------------------
# Tick result
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TickResult:
    """What happened during one ``run_once()`` call.

    The four ``transitions``/``incidents_*`` fields come straight from
    Milestone 6's ``IncidentProcessingResult`` -- see
    ``argus.incidents.engine`` -- and are always ``0`` on a failed tick,
    same as the discovery counts above them.
    """

    success: bool
    applications: int
    services: int
    containers: int
    observations: int
    skipped: int
    transitions_created: int = 0
    incidents_opened: int = 0
    incidents_updated: int = 0
    incidents_resolved: int = 0
    error: Optional[str] = None


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


class CollectorLoop:
    """Schedules ``discover() -> evaluate -> persist_discovery()`` on a timer.

    ``clock`` and ``sleep`` are injected specifically so tests never
    wait on real time: default to the real wall clock / ``time.sleep``
    for live use, and to deterministic fakes in tests.
    """

    def __init__(
        self,
        *,
        client: DockerClient,
        repository: Repository,
        config: CollectorConfig = DEFAULT_COLLECTOR_CONFIG,
        rules: HealthRules = DEFAULT_HEALTH_RULES,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._docker_client = client
        self._repository = repository
        self._config = config
        self._rules = rules
        self._clock = clock
        self._sleep = sleep

    # -----------------------------------------------------------------
    # One tick
    # -----------------------------------------------------------------

    def run_once(self) -> TickResult:
        """One full, deterministic collection attempt.

        Successful tick:
            1. establish this tick's timestamp (one clock read, reused
               throughout -- for `observed_at`, `last_tick_at`, and
               `last_success_at` alike)
            2. record that a tick started
            3. call Docker discovery once (`discover()` -- Milestone 3)
            4. resolve each Observation's real, already-computed health
               (`DiscoveryResult.evaluations`) onto it
            5. persist the whole snapshot in one transaction
               (`persist_discovery()` -- Milestone 4)
            6. detect transitions and update incidents, in a second
               transaction (`process_transitions_and_incidents()` --
               Milestone 6)
            7. record the tick as successful

        A `DiscoveryResult` containing `skipped` entries (a malformed
        or vanished container Milestone 3 already isolated) is still a
        *successful* tick -- that isolation is exactly what Milestone 3
        was built to provide, and surfacing those skips as warnings is
        deferred to a later milestone, not treated as a tick failure
        here.

        If transition/incident processing fails, the tick fails too --
        `last_success_at` is not advanced, exactly like a
        `persist_discovery` failure. The observations from step 5 remain
        persisted (that transaction already committed); only this
        tick's transition/incident bookkeeping is missing, and the very
        next successful tick's comparison against the database catches
        up correctly on its own -- see the Milestone 6 report for the
        full reasoning on this transaction boundary.

        `discover()` is given a `history_provider` backed by this tick's
        own `self._repository` (see `_history_provider`), so restart-loop
        / recent-restart detection actually sees real, persisted history
        across polls here -- unlike a bare one-shot `discover()` call
        (Milestone 3's own tests), which by construction has none to
        supply. This is a Milestone 9 correction: prior to it, this
        method called `discover()` with no history provider at all, so
        `prior_observations` was always empty on every real tick and
        `_restart_delta_within_window` could never observe more than one
        sample -- restart-loop/recent-restart classification was
        silently unreachable outside of `discover()`'s own direct unit
        tests. See the Milestone 9 completion report for the full
        writeup.
        """

        tick_at = self._clock()
        self._repository.record_tick_started(at=tick_at)

        try:
            result = discover(
                self._docker_client,
                observed_at=tick_at,
                rules=self._rules,
                history_provider=self._history_provider(tick_at),
            )
        except DockerUnavailableError as exc:
            return self._fail(exc)

        try:
            resolved_observations = self._resolve_observations(result)
        except MissingEvaluationError as exc:
            return self._fail(exc)

        try:
            self._repository.persist_discovery(
                applications=result.applications, observations=resolved_observations
            )
        except PersistenceError as exc:
            return self._fail(exc)

        try:
            container_statuses = {
                container_id: evaluation.status for container_id, evaluation in result.evaluations.items()
            }
            incident_result = process_transitions_and_incidents(
                repository=self._repository,
                applications=result.applications,
                container_statuses=container_statuses,
                occurred_at=tick_at,
            )
        except (IncidentProcessingError, PersistenceError) as exc:
            return self._fail(exc)

        self._repository.record_tick_success(at=tick_at)

        if result.skipped:
            logger.warning(
                "collector tick succeeded with %d skipped container(s)/application(s)",
                len(result.skipped),
            )
        logger.info(
            "collector tick succeeded: %d application(s), %d observation(s), "
            "%d transition(s), %d incident(s) opened/%d updated/%d resolved",
            len(result.applications),
            len(resolved_observations),
            incident_result.transitions_created,
            incident_result.incidents_opened,
            incident_result.incidents_updated,
            incident_result.incidents_resolved,
        )

        return TickResult(
            success=True,
            applications=len(result.applications),
            services=sum(len(a.services) for a in result.applications),
            containers=sum(len(s.containers) for a in result.applications for s in a.services),
            observations=len(resolved_observations),
            skipped=len(result.skipped),
            transitions_created=incident_result.transitions_created,
            incidents_opened=incident_result.incidents_opened,
            incidents_updated=incident_result.incidents_updated,
            incidents_resolved=incident_result.incidents_resolved,
            error=None,
        )

    def _history_provider(self, tick_at: datetime) -> Callable[[str], Sequence[Observation]]:
        """Build a `discover()` history provider bound to this one tick.

        Bounded to just over `self._rules.restart_loop_window` before
        `tick_at`, not a container's entire lifetime -- `discover()`'s own
        `_restart_delta_within_window` (via `evaluate_container_health`)
        re-filters to the exact inclusive window anyway, so this bound is
        purely an efficiency cutoff, never a correctness one; the extra
        second of margin exists solely so a prior observation sitting
        exactly on the window's own boundary is never excluded here
        before that precise re-filtering gets to see it.
        """

        lookback = tick_at - timedelta(seconds=self._rules.restart_loop_window + 1)

        def provider(container_id: str) -> Sequence[Observation]:
            return self._repository.get_observations_after(container_id, after=lookback)

        return provider

    def _resolve_observations(self, result) -> list[Observation]:
        """Bridge each Observation with its already-computed HealthEvaluation.

        Milestone 3's `discover()` always constructs an `Observation`
        with a placeholder `derived_status=UNKNOWN` -- health hasn't
        been evaluated yet at construction time, and it's frozen. The
        real status/detail live separately, in
        `DiscoveryResult.evaluations`, keyed by container id.
        `resolve_observation_health` (owned by `argus.store.repository`
        -- see the Milestone 5 report for why it stays there) performs
        the actual field swap; this method's only job is the lookup and
        the "no match -> fail the tick" policy, since persisting an
        Observation Argus never actually evaluated would be more
        misleading than not persisting it at all.
        """

        resolved: list[Observation] = []
        for observation in result.observations:
            container_id = observation.container_ref.container_id
            evaluation = result.evaluations.get(container_id)
            if evaluation is None:
                raise MissingEvaluationError(
                    f"no HealthEvaluation for container {container_id!r}; DiscoveryResult is "
                    "internally inconsistent -- refusing to persist a fabricated status"
                )
            resolved.append(
                resolve_observation_health(
                    observation, status=evaluation.status, detail=evaluation.detail
                )
            )
        return resolved

    def _fail(self, exc: BaseException) -> TickResult:
        message = _sanitize_error(exc)
        logger.warning("collector tick failed: %s", message)
        try:
            self._repository.record_tick_failure(error=message)
        except PersistenceError:
            # The database itself may be the thing that's unavailable --
            # log it and keep the loop alive rather than crash trying to
            # record that we couldn't record a failure.
            logger.error("could not record tick failure to collector_state (database unavailable?)")
        return TickResult(
            success=False,
            applications=0,
            services=0,
            containers=0,
            observations=0,
            skipped=0,
            error=message,
        )

    # -----------------------------------------------------------------
    # Scheduling
    # -----------------------------------------------------------------

    def run_forever(
        self,
        *,
        stop_after_n_ticks: Optional[int] = None,
        stop: Optional[Callable[[], bool]] = None,
    ) -> None:
        """Tick, sleep, tick, sleep, ... until told to stop.

        The stop condition is checked only at the top of the loop, so
        every tick this method runs is always followed by exactly one
        sleep before the next stop check -- there is no "tick without a
        following sleep" path. A successful tick sleeps
        `config.poll_interval`; a failed one sleeps
        `compute_backoff(consecutive_failures)`.

        Any exception `run_once()` itself doesn't already turn into a
        `TickResult` (i.e. a genuine bug, not one of the three handled
        failure categories) is caught here, logged at ERROR with a full
        traceback, and recorded as a failed tick -- a single tick's bug
        must not take the whole long-running process down. This is a
        deliberate outer safety net around scheduling, not a license
        for `run_once()`'s own internals to swallow errors mid-work.
        """

        ticks = 0
        while True:
            if stop is not None and stop():
                break
            if stop_after_n_ticks is not None and ticks >= stop_after_n_ticks:
                break

            try:
                result = self.run_once()
            except Exception as exc:  # noqa: BLE001 -- deliberate outer safety net, see docstring
                logger.exception("collector tick raised an unhandled exception")
                result = self._fail(exc)

            ticks += 1

            if result.success:
                wait = self._config.poll_interval
            else:
                state = self._repository.get_collector_state()
                wait = compute_backoff(state.consecutive_failures, self._config)
            self._sleep(wait)
