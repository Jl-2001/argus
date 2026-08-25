"""The long-running collector loop.

Wires together these already-complete layers on a timer:

    argus.collectors.docker_collector.discover()   -- Milestone 3
    (the evaluations discover() already computed)  -- Milestone 2, via M3
    argus.store.repository.Repository.persist_discovery()  -- Milestone 4
    argus.evidence (log/evidence collection)        -- Milestone 10
    argus.incidents.engine.process_transitions_and_incidents()  -- Milestone 6
    argus.evidence.association (incident <-> evidence linking)  -- Milestone 10

This module contains no discovery logic, no health rules, no
persistence mechanics, and no evidence-classification logic of its own
-- only scheduling, ordering, failure classification, and backoff.
``run_once()`` is one deterministic collection attempt; ``run_forever()``
is the scheduling wrapper around it. Tests target ``run_once()`` almost
exclusively, with a clock and a sleep function both injected so nothing
here depends on real wall-clock time.

Tick ordering (Milestone 10 extends the Milestone 6 pipeline)::

    1. discover -> evaluate
    2. persist observations                  (core monitoring truth)
    3. collect + persist evidence             (auxiliary -- see below)
    4. detect transitions, open/resolve incidents
    5. associate evidence with incidents      (auxiliary -- see below)

Steps 3 and 5 are evidence collection's two halves, and both are
*isolated* from core monitoring: a Docker log read failing for one
container, an evidence-persistence error, or an association-query
failure never fails the tick (`TickResult.success` is untouched by
either) -- it is recorded as its own, separate failure via
`Repository.record_evidence_tick_failure`, exactly so a problem in the
auxiliary evidence subsystem can never make core health monitoring look
down. Evidence collection deliberately runs *after* observations are
persisted (so `application_id`/`container_id` row ids already exist to
attach evidence to) but *before* transition/incident detection --
placing it any later would mean incident detection never sees evidence
collected in the very same tick a status changed. Association
deliberately runs *after* transition/incident detection, since it needs
to know this tick's own incident open/resolve outcome to compute each
incident's current window.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Sequence

from argus.collectors.docker_client import DockerClient, DockerUnavailableError
from argus.collectors.docker_collector import discover
from argus.domain.health import DEFAULT_HEALTH_RULES, HealthRules
from argus.domain.host import LOCAL_HOST_KEY
from argus.domain.models import DockerHealth, EvidenceCategory, Observation
from argus.evidence.association import DEFAULT_ASSOCIATION_WINDOW_SECONDS, associate_evidence
from argus.evidence.collector import (
    DEFAULT_EVIDENCE_LIMITS,
    EvidenceCollectionLimits,
    collect_evidence_for_container,
    docker_fact_evidence,
)
from argus.evidence.persistence import persist_candidates
from argus.incidents.engine import IncidentProcessingError
from argus.ingestion.pipeline import persist_snapshot, process_incidents_for_snapshot
from argus.realtime.emitter import emit_collector_tick, emit_evidence_health_changed, emit_evidence_updated
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

    #: Milestone 10 -- evidence collection. ``collect_evidence=False`` is
    #: a full kill-switch (skips steps 3/5 entirely, e.g. for a v0.1-only
    #: deployment that isn't ready for the extra Docker log reads yet);
    #: the rest tune the bounds ``argus.evidence.collector`` and
    #: ``argus.evidence.association`` apply. None of these are folded
    #: into `HealthRules` -- they configure a different subsystem
    #: entirely (what evidence to collect, not what health status to
    #: compute), so conflating the two would make both harder to reason
    #: about independently.
    collect_evidence: bool = True
    evidence_limits: EvidenceCollectionLimits = field(default_factory=lambda: DEFAULT_EVIDENCE_LIMITS)
    evidence_association_window_seconds: int = DEFAULT_ASSOCIATION_WINDOW_SECONDS
    evidence_retention_days: int = 14

    def __post_init__(self) -> None:
        if self.poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if self.backoff_initial <= 0:
            raise ValueError("backoff_initial must be positive")
        if self.backoff_max < self.backoff_initial:
            raise ValueError("backoff_max must be >= backoff_initial")
        if self.evidence_retention_days <= 0:
            raise ValueError("evidence_retention_days must be positive")


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

    ``evidence_signals_created``/``evidence_associations`` and
    ``evidence_error`` (Milestone 10) are deliberately independent of
    ``success``/``error`` above: an evidence-subsystem failure never
    fails the tick (see this module's docstring), so
    ``evidence_error is not None`` can be true even when
    ``success is True``. Conversely, on a failed *core* tick (discovery/
    persistence/transitions), evidence collection never ran at all this
    tick, so both evidence fields are ``0``/``None`` regardless of the
    evidence subsystem's own health.
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
    evidence_signals_created: int = 0
    evidence_associations: int = 0
    evidence_error: Optional[str] = None
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
        host_display_name: str = "Local Host",
    ) -> None:
        self._docker_client = client
        self._repository = repository
        self._config = config
        self._rules = rules
        self._clock = clock
        self._sleep = sleep
        # Milestone 16 -- resolved lazily (see `_local_host_id`), not
        # here: `repository` is already open by construction time in
        # every real caller, but resolving it eagerly would mean every
        # existing test building a `CollectorLoop` starts writing a
        # `hosts` row it never asked for, even for tests that never call
        # `run_once`.
        self._host_display_name = host_display_name
        self._host_id: Optional[int] = None

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
        host_id = self._local_host_id(tick_at=tick_at)
        self._repository.record_tick_started(at=tick_at)

        try:
            result = discover(
                self._docker_client,
                observed_at=tick_at,
                rules=self._rules,
                history_provider=self._history_provider(tick_at),
            )
        except DockerUnavailableError as exc:
            return self._fail(exc, tick_at=tick_at)

        try:
            resolved_observations = self._resolve_observations(result)
        except MissingEvaluationError as exc:
            return self._fail(exc, tick_at=tick_at)

        # Captured *before* persisting this tick's new observations, so
        # "before" genuinely means the previous tick's reading -- used
        # only for Milestone 10's restart-count-delta evidence below.
        previous_observation_by_container_id = {
            observation.container_ref.container_id: self._repository.get_latest_observation(
                observation.container_ref.container_id
            )
            for observation in resolved_observations
        }

        # Step 5 (persist_discovery) runs through the same shared
        # pipeline `argus.api.routes.agents` calls for a remote agent's
        # snapshot -- see `argus.ingestion.pipeline`'s own docstring for
        # why this must not be two separately-maintained copies of the
        # same sequence. `host_key=LOCAL_HOST_KEY` makes the
        # application-key host-scoping step inside it a complete no-op
        # for this, the local collector's own call -- every existing
        # single-host application key is unaffected. `scoped_applications`
        # (identical objects to `result.applications` here, since the
        # scoping is a no-op) is what every step below must use, not
        # `result.applications` -- see `persist_snapshot`'s own docstring.
        try:
            persist_report, scoped_applications = persist_snapshot(
                self._repository,
                host_id=host_id,
                host_key=LOCAL_HOST_KEY,
                applications=result.applications,
                observations=resolved_observations,
            )
        except PersistenceError as exc:
            return self._fail(exc, tick_at=tick_at)

        evidence_signals_created, evidence_error = self._collect_and_persist_evidence(
            result, resolved_observations, previous_observation_by_container_id, tick_at
        )

        # Step 6: transition/incident detection -- kept as its own,
        # separately-failable step exactly as before this refactor (a
        # failure here still leaves this tick's already-persisted
        # observations in place; only its transition/incident
        # bookkeeping is missing -- see the Milestone 6 report).
        try:
            incident_result = process_incidents_for_snapshot(
                self._repository,
                applications=scoped_applications,
                observations=resolved_observations,
                tick_at=tick_at,
            )
        except (IncidentProcessingError, PersistenceError) as exc:
            return self._fail(exc, tick_at=tick_at)

        evidence_associations, association_error = self._associate_evidence(tick_at)

        # Exactly one evidence-heartbeat update per tick, combining both
        # halves' outcomes -- see _associate_evidence's own docstring for
        # why this must not be two separate record_evidence_tick_*
        # calls. Collection's own error takes precedence when both
        # halves failed (it ran first, and association's own failure is
        # often just a downstream consequence of nothing new to find).
        #
        # When `collect_evidence` is off, the heartbeat is left
        # completely untouched (no success, no failure) -- recording a
        # "success" for a subsystem that was never even asked to run
        # would misrepresent it as active; `None`/`0` already means
        # "never run" (see `CollectorStateRecord`'s own docstring), which
        # is the honest state here.
        combined_evidence_error = evidence_error if evidence_error is not None else association_error
        if self._config.collect_evidence:
            evidence_was_healthy = self._repository.get_collector_state().consecutive_evidence_failures == 0
            if combined_evidence_error is None:
                self._repository.record_evidence_tick_success(at=tick_at)
            else:
                try:
                    self._repository.record_evidence_tick_failure(error=combined_evidence_error)
                except PersistenceError:
                    logger.error("could not record evidence tick failure to collector_state (database unavailable?)")
            evidence_is_healthy = self._repository.get_collector_state().consecutive_evidence_failures == 0
            if evidence_is_healthy != evidence_was_healthy:
                emit_evidence_health_changed(self._repository, healthy=evidence_is_healthy, tick_at=tick_at, now=tick_at)

        emit_evidence_updated(
            self._repository, signals_created=evidence_signals_created, associations=evidence_associations,
            tick_at=tick_at, now=tick_at,
        )

        self._repository.record_tick_success(at=tick_at)
        # The local host's own heartbeat -- mirrors exactly what
        # `argus.api.routes.agents` does for a remote agent on every
        # successfully-authenticated ingest, so `GET /api/v1/hosts`'s
        # ONLINE/STALE/OFFLINE classification (`argus.domain.host
        # .evaluate_host_status`) reflects reality for the local host
        # too, not just remote ones -- without this, a local host's
        # `last_seen_at` would only ever be stamped once, at bootstrap,
        # and every long-running Argus install would eventually show
        # its own local host as OFFLINE despite ticking successfully.
        self._repository.record_host_heartbeat(host_id=host_id, at=tick_at)
        emit_collector_tick(
            self._repository, success=True, tick_at=tick_at,
            applications=len(result.applications), observations=len(resolved_observations), now=tick_at,
        )

        if result.skipped:
            logger.warning(
                "collector tick succeeded with %d skipped container(s)/application(s)",
                len(result.skipped),
            )
        logger.info(
            "collector tick succeeded: %d application(s), %d observation(s), "
            "%d transition(s), %d incident(s) opened/%d updated/%d resolved, "
            "%d evidence signal(s), %d evidence association(s)",
            len(result.applications),
            len(resolved_observations),
            incident_result.transitions_created,
            incident_result.incidents_opened,
            incident_result.incidents_updated,
            incident_result.incidents_resolved,
            evidence_signals_created,
            evidence_associations,
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
            evidence_signals_created=evidence_signals_created,
            evidence_associations=evidence_associations,
            evidence_error=combined_evidence_error,
            error=None,
        )

    def _local_host_id(self, *, tick_at: datetime) -> int:
        """Resolves (and caches, for the lifetime of this loop) the
        local host's own row id -- see ``Repository.ensure_local_host``.
        Called once per tick rather than once at construction so a
        loop's ``host_display_name`` (e.g. read from ``ARGUS_HOST_NAME``
        at process startup) is applied the very first time this loop
        actually does anything, not silently skipped for a loop object
        constructed before the database was ready.

        Takes ``tick_at`` (this tick's own, already-read clock value)
        rather than reading ``self._clock()`` itself -- ``run_once``'s
        one-clock-read-per-tick discipline (see its own docstring) must
        hold even on the very first tick, when this method also happens
        to bootstrap the local host row; a second, independent clock
        read here would silently consume an extra value from an
        injected test clock and desynchronize every subsequent
        ``self._clock()`` call in the same tick from the caller's own
        expectations -- exactly the bug a real Milestone 16 regression
        test caught (see ``tests/integration/test_chaos_stack.py``'s
        own ``TestTransitionTimestampAccuracyEndToEnd``).
        """

        if self._host_id is None:
            self._host_id = self._repository.ensure_local_host(
                display_name=self._host_display_name, now=tick_at
            )
        return self._host_id

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

    def _collect_and_persist_evidence(
        self,
        result,
        resolved_observations: list[Observation],
        previous_observation_by_container_id: dict[str, Optional[Observation]],
        tick_at: datetime,
    ) -> tuple[int, Optional[str]]:
        """Milestone 10, tick step 3: collect and persist evidence for
        every container this tick successfully observed.

        Deliberately never raises -- any failure here is caught,
        recorded via ``record_evidence_tick_failure`` (its own,
        independent heartbeat -- see ``CollectorStateRecord``), and
        reported back as ``(0, error_message)`` rather than failing the
        whole tick. A single container's Docker log read failing is
        *already* isolated one layer down, inside
        ``collect_evidence_for_container`` itself; the broad
        ``except Exception`` here exists for the layer above that --
        e.g. a genuinely unexpected bug in this method's own
        orchestration, or a repository write failing -- exactly
        mirroring the documented, deliberate outer safety net
        ``run_forever`` already uses around a whole tick, scoped here to
        just the evidence subsystem so a bug in it can never make core
        health monitoring look down.

        Returns ``(signals_created, error_message_or_None)``.
        """

        if not self._config.collect_evidence:
            return 0, None

        try:
            signals_created = 0
            limits = self._config.evidence_limits
            resolved_by_container_id = {
                observation.container_ref.container_id: observation for observation in resolved_observations
            }

            containers_capped = False
            for application in result.applications:
                if containers_capped:
                    break
                application_record = self._repository.get_application(application.key)
                if application_record is None:
                    continue

                for service in application.services:
                    if containers_capped:
                        break
                    for container in service.containers:
                        if signals_created >= limits.max_signals_per_tick:
                            logger.warning(
                                "evidence signal cap (%d) reached this tick -- remaining "
                                "container(s) will be collected on a later tick, not skipped "
                                "permanently; no cursor was advanced for them",
                                limits.max_signals_per_tick,
                            )
                            containers_capped = True
                            break

                        container_id = container.container_id
                        observation = resolved_by_container_id.get(container_id)
                        if observation is None:
                            continue
                        container_record = self._repository.get_container_by_docker_id(container_id)
                        if container_record is None:
                            continue

                        cursor_after = self._repository.get_log_cursor(container_record.id)
                        log_result = collect_evidence_for_container(
                            self._docker_client, container_id,
                            cursor_after=cursor_after, tick_at=tick_at, limits=limits,
                        )
                        if log_result.error is not None:
                            logger.warning(
                                "evidence log collection skipped for container %s this tick: %s",
                                container_id, log_result.error,
                            )

                        if log_result.candidates:
                            signals_created += persist_candidates(
                                self._repository, list(log_result.candidates),
                                application_id=application_record.id, container_row_id=container_record.id,
                                source_type="container_log", source_ref="stdout+stderr",
                                aggregation_window_seconds=limits.aggregation_window_seconds,
                            )
                        if log_result.new_cursor_at is not None:
                            self._repository.set_log_cursor(
                                container_record.id, last_log_at=log_result.new_cursor_at, updated_at=tick_at
                            )

                        previous_observation = previous_observation_by_container_id.get(container_id)
                        fact_candidates = docker_fact_evidence(
                            observed_at=tick_at,
                            restart_count_before=(
                                previous_observation.restart_count if previous_observation is not None else None
                            ),
                            restart_count_after=observation.restart_count,
                            docker_health_is_unhealthy=(observation.docker_health is DockerHealth.UNHEALTHY),
                        )
                        for candidate in fact_candidates:
                            source_ref = (
                                "restart_count" if candidate.category is EvidenceCategory.CONTAINER_RESTART
                                else "docker_health"
                            )
                            signals_created += persist_candidates(
                                self._repository, [candidate],
                                application_id=application_record.id, container_row_id=container_record.id,
                                source_type="docker_fact", source_ref=source_ref,
                                aggregation_window_seconds=limits.aggregation_window_seconds,
                            )

            retention_cutoff = tick_at - timedelta(days=self._config.evidence_retention_days)
            self._repository.delete_expired_log_signals(before=retention_cutoff)

            return signals_created, None
        except Exception as exc:  # noqa: BLE001 -- deliberate, documented, scoped safety net; see docstring
            message = _sanitize_error(exc)
            logger.warning("evidence collection failed this tick (core monitoring unaffected): %s", message)
            return 0, message

    def _associate_evidence(self, tick_at: datetime) -> tuple[int, Optional[str]]:
        """Milestone 10, tick step 5: link evidence to incidents by time
        proximity (see ``argus.evidence.association``). Runs after
        transition/incident detection, since it needs this tick's own
        open/resolve outcome. Never raises, for the same reason as
        ``_collect_and_persist_evidence``.

        Returns ``(associations_made, error_message_or_None)`` -- the
        caller (``run_once``) combines this with
        ``_collect_and_persist_evidence``'s own result into exactly
        *one* evidence-heartbeat update per tick (see ``run_once``'s own
        comment on why: recording two separate failures for what is
        conceptually one tick's evidence subsystem would double-count
        `consecutive_evidence_failures`).
        """

        if not self._config.collect_evidence:
            return 0, None

        try:
            associations = associate_evidence(
                self._repository, now=tick_at,
                window_seconds=self._config.evidence_association_window_seconds,
            )
            return associations, None
        except Exception as exc:  # noqa: BLE001 -- deliberate, documented, scoped safety net; see docstring above
            message = _sanitize_error(exc)
            logger.warning("evidence association failed this tick (core monitoring unaffected): %s", message)
            return 0, message

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

    def _fail(self, exc: BaseException, *, tick_at: Optional[datetime] = None) -> TickResult:
        message = _sanitize_error(exc)
        logger.warning("collector tick failed: %s", message)
        resolved_tick_at = tick_at if tick_at is not None else self._clock()
        try:
            self._repository.record_tick_failure(error=message)
        except PersistenceError:
            # The database itself may be the thing that's unavailable --
            # log it and keep the loop alive rather than crash trying to
            # record that we couldn't record a failure. Since the
            # failure state itself couldn't be persisted, no
            # `collector.tick` event is emitted either -- see
            # argus.realtime.emitter's own "emit-after-commit-only" rule.
            logger.error("could not record tick failure to collector_state (database unavailable?)")
        else:
            emit_collector_tick(
                self._repository, success=False, tick_at=resolved_tick_at,
                applications=0, observations=0, now=resolved_tick_at,
            )
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
