"""``AgentCollector`` -- builds one ``AgentSnapshot`` per poll.

Reuses exactly the same discovery/health/evidence code the local
collector uses (``argus.collectors.docker_collector.discover``,
``argus.evidence.collector``) -- nothing about *how* Docker facts
become health statuses or evidence signals is reimplemented here. What
this module owns is the bounded, in-*memory* (never persisted to disk
on the agent host -- see the milestone's own "does not persist
gigabytes of offline logs locally") state a stateless-between-restarts
agent still needs across polls: recent observation history (for
restart-loop detection) and per-container log-read cursors (so the same
lines aren't re-read, re-classified, and re-sent every poll).
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence

from argus.agent.protocol import MAX_EVIDENCE_ITEMS_PER_SNAPSHOT, EvidenceCandidateWire
from argus.collectors.docker_client import DockerClient
from argus.collectors.docker_collector import discover
from argus.domain.health import DEFAULT_HEALTH_RULES, HealthRules
from argus.domain.models import Application, DockerHealth, EvidenceCategory, Observation
from argus.evidence.collector import (
    DEFAULT_EVIDENCE_LIMITS,
    EvidenceCollectionLimits,
    collect_evidence_for_container,
    docker_fact_evidence,
)

__all__ = ["AgentSnapshotResult", "AgentCollector"]


def _resolve_observation_health(observation: Observation, *, status, detail) -> Observation:
    """A small, local equivalent of
    ``argus.store.repository.resolve_observation_health`` -- deliberately
    not imported from ``argus.store`` (the agent package has no reason
    to depend on the control plane's persistence layer at all; see
    ``argus.agent``'s own docstring on its import boundary). Same
    mechanical field-copy, nothing more."""

    return dataclasses.replace(observation, derived_status=status, derived_detail=detail)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentSnapshotResult:
    applications: tuple[Application, ...]
    observations: tuple[Observation, ...]
    evidence_candidates: tuple[EvidenceCandidateWire, ...]
    skipped: int


class AgentCollector:
    """One instance lives for the whole life of the ``argus-agent``
    process -- its two small in-memory dicts (bounded: one recent-
    observation list and one cursor timestamp per currently-seen
    container, nothing unbounded, nothing that grows across restarts
    since it starts empty every time) are exactly what let this
    otherwise-stateless process still get real restart-loop detection
    and non-duplicated log evidence across polls, without a local
    database.
    """

    def __init__(
        self,
        *,
        client: DockerClient,
        rules: HealthRules = DEFAULT_HEALTH_RULES,
        evidence_limits: EvidenceCollectionLimits = DEFAULT_EVIDENCE_LIMITS,
    ) -> None:
        self._client = client
        self._rules = rules
        self._evidence_limits = evidence_limits
        self._observation_history: dict[str, list[Observation]] = {}
        self._log_cursors: dict[str, datetime] = {}

    def collect_snapshot(self, *, now: datetime) -> AgentSnapshotResult:
        result = discover(
            self._client,
            observed_at=now,
            rules=self._rules,
            history_provider=lambda container_id: tuple(self._observation_history.get(container_id, ())),
        )

        resolved_observations: list[Observation] = []
        for observation in result.observations:
            container_id = observation.container_ref.container_id
            evaluation = result.evaluations.get(container_id)
            if evaluation is None:
                continue  # structurally inconsistent -- skip, mirroring CollectorLoop's own strictness
            resolved_observations.append(
                _resolve_observation_health(observation, status=evaluation.status, detail=evaluation.detail)
            )

        # Captured before this tick's own observations are folded into
        # history below -- the same "before" `CollectorLoop` itself
        # relies on for restart-count-delta evidence.
        previous_observation_by_container_id: dict[str, Optional[Observation]] = {
            observation.container_ref.container_id: (
                self._observation_history.get(observation.container_ref.container_id, [None])[-1]
                if self._observation_history.get(observation.container_ref.container_id)
                else None
            )
            for observation in resolved_observations
        }

        evidence_candidates = self._collect_evidence(
            result.applications, resolved_observations, previous_observation_by_container_id, now
        )

        self._update_history(resolved_observations, now)

        return AgentSnapshotResult(
            applications=result.applications,
            observations=tuple(resolved_observations),
            evidence_candidates=evidence_candidates,
            skipped=len(result.skipped),
        )

    def _update_history(self, resolved_observations: Sequence[Observation], now: datetime) -> None:
        cutoff = now - timedelta(seconds=self._rules.restart_loop_window + 1)
        seen_container_ids = set()
        for observation in resolved_observations:
            container_id = observation.container_ref.container_id
            seen_container_ids.add(container_id)
            bucket = self._observation_history.setdefault(container_id, [])
            bucket.append(observation)
            bucket[:] = [o for o in bucket if o.observed_at >= cutoff]
        # A container no longer discovered at all (removed/renamed) has
        # nothing left to bound -- drop its history outright rather than
        # let a vanished container's entry sit in memory forever.
        for stale_id in set(self._observation_history) - seen_container_ids:
            del self._observation_history[stale_id]
            self._log_cursors.pop(stale_id, None)

    def _collect_evidence(
        self,
        applications: Sequence[Application],
        resolved_observations: Sequence[Observation],
        previous_observation_by_container_id: dict[str, Optional[Observation]],
        now: datetime,
    ) -> tuple[EvidenceCandidateWire, ...]:
        resolved_by_container_id = {o.container_ref.container_id: o for o in resolved_observations}
        candidates: list[EvidenceCandidateWire] = []

        for application in applications:
            for service in application.services:
                for container in service.containers:
                    if len(candidates) >= MAX_EVIDENCE_ITEMS_PER_SNAPSHOT:
                        logger.warning(
                            "agent evidence cap (%d) reached this poll -- remaining container(s) "
                            "will be collected on a later poll, not skipped permanently",
                            MAX_EVIDENCE_ITEMS_PER_SNAPSHOT,
                        )
                        return tuple(candidates)

                    container_id = container.container_id
                    observation = resolved_by_container_id.get(container_id)
                    if observation is None:
                        continue

                    try:
                        log_result = collect_evidence_for_container(
                            self._client, container_id,
                            cursor_after=self._log_cursors.get(container_id),
                            tick_at=now, limits=self._evidence_limits,
                        )
                    except Exception:  # noqa: BLE001 -- one container's evidence failure never stops the poll
                        logger.warning("agent evidence collection failed for container %s", container_id)
                        continue

                    if log_result.new_cursor_at is not None:
                        self._log_cursors[container_id] = log_result.new_cursor_at

                    for signal in log_result.candidates:
                        candidates.append(
                            EvidenceCandidateWire(
                                application_key=application.key,
                                container_id=container_id,
                                category=signal.category,
                                severity=signal.severity,
                                normalized_signature=signal.normalized_signature,
                                first_seen_at=signal.first_seen_at,
                                last_seen_at=signal.last_seen_at,
                                count=signal.count,
                                sample=signal.sample,
                                source_type="container_log",
                                source_ref="stdout+stderr",
                            )
                        )

                    previous_observation = previous_observation_by_container_id.get(container_id)
                    for fact in docker_fact_evidence(
                        observed_at=now,
                        restart_count_before=(
                            previous_observation.restart_count if previous_observation is not None else None
                        ),
                        restart_count_after=observation.restart_count,
                        docker_health_is_unhealthy=(observation.docker_health is DockerHealth.UNHEALTHY),
                    ):
                        source_ref = (
                            "restart_count" if fact.category is EvidenceCategory.CONTAINER_RESTART
                            else "docker_health"
                        )
                        candidates.append(
                            EvidenceCandidateWire(
                                application_key=application.key,
                                container_id=container_id,
                                category=fact.category,
                                severity=fact.severity,
                                normalized_signature=fact.normalized_signature,
                                first_seen_at=fact.first_seen_at,
                                last_seen_at=fact.last_seen_at,
                                count=fact.count,
                                sample=fact.sample,
                                source_type="docker_fact",
                                source_ref=source_ref,
                            )
                        )

        return tuple(candidates)
