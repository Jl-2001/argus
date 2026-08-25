"""The one shared pipeline that turns a batch of already-evaluated
domain facts (applications + observations, from *either* the local
collector's own Docker discovery or a remote agent's ingested snapshot)
into persisted identity rows, transitions, and incidents.

Milestone 16's own "Central vs Local Collection" requirement is what
this module exists to satisfy: "Avoid two completely separate incident
pipelines." Before this milestone, ``argus.collector.loop.CollectorLoop
.run_once`` called ``Repository.persist_discovery`` and
``argus.incidents.engine.process_transitions_and_incidents`` directly,
inline. Milestone 16 needed a second caller (the agent ingestion route)
to do the exact same two calls in the exact same order for a remote
snapshot -- rather than let that route grow its own copy of this
sequence, both calls were factored out here, and ``CollectorLoop`` was
refactored to call these functions too (see its own comment at the two
call sites).

Split into two functions, not one, so ``CollectorLoop`` can keep its
existing, documented tick ordering exactly -- evidence collection (a
live Docker log read, something only the local collector does; an
agent already collected and redacted its own evidence before sending
it) runs *between* persisting a snapshot and processing its incidents,
not before both or after both. ``persist_snapshot_and_process_incidents``
below is the convenience most callers (the agent ingestion route, which
has no such step in between) actually want.

This module also owns the one place ``argus.domain.host
.scope_application_key`` is actually *applied* to a batch of
``Application``/``Service`` objects -- see `rescope_applications_for_host`.
Every other module downstream of this one (``Repository``,
``process_transitions_and_incidents``, ``argus.realtime.emitter``) sees
only already-scoped keys and has no idea a host concept exists.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Sequence

from argus.domain.host import LOCAL_HOST_KEY, scope_application_key
from argus.domain.models import Application, Observation
from argus.incidents.engine import IncidentProcessingResult, process_transitions_and_incidents
from argus.realtime.emitter import emit_incident_processing_events
from argus.store.repository import PersistDiscoveryReport, Repository

__all__ = [
    "rescope_applications_for_host",
    "persist_snapshot",
    "process_incidents_for_snapshot",
    "persist_snapshot_and_process_incidents",
]


def rescope_applications_for_host(
    applications: Sequence[Application], *, host_key: str
) -> tuple[Application, ...]:
    """Rewrites every ``Application.key`` (and each of its ``Service
    .application_key``, which must always match -- see
    ``Application.__post_init__``) through
    ``argus.domain.host.scope_application_key``.

    A no-op (returns ``applications`` as a plain tuple, no new objects
    built) for the local host -- this is the exact mechanism that keeps
    a single-host installation's application keys byte-for-byte
    unchanged; see ``argus.domain.host.LOCAL_HOST_KEY``.
    """

    if host_key == LOCAL_HOST_KEY:
        return tuple(applications)

    rescoped: list[Application] = []
    for application in applications:
        new_key = scope_application_key(host_key, application.key)
        new_services = tuple(
            dataclasses.replace(service, application_key=new_key) for service in application.services
        )
        rescoped.append(dataclasses.replace(application, key=new_key, services=new_services))
    return tuple(rescoped)


def persist_snapshot(
    repository: Repository,
    *,
    host_id: int,
    host_key: str,
    applications: Sequence[Application],
    observations: Sequence[Observation],
) -> tuple[PersistDiscoveryReport, tuple[Application, ...]]:
    """Host-scopes ``applications`` (see `rescope_applications_for_host`)
    and persists the whole snapshot in one transaction (see
    ``Repository.persist_discovery``).

    Returns the *rescoped* applications alongside the persistence
    report -- the caller's next step (`process_incidents_for_snapshot`,
    possibly with evidence collection of its own in between) must use
    these exact objects, not the originally-supplied ones, so that
    every downstream ``application_key`` (transitions, incidents,
    realtime events) is consistently the persisted, host-scoped key.
    """

    scoped_applications = rescope_applications_for_host(applications, host_key=host_key)
    report = repository.persist_discovery(
        applications=scoped_applications, observations=observations, host_id=host_id
    )
    return report, scoped_applications


def process_incidents_for_snapshot(
    repository: Repository,
    *,
    applications: Sequence[Application],
    observations: Sequence[Observation],
    tick_at: datetime,
) -> IncidentProcessingResult:
    """Detects transitions and updates incident lifecycle for one
    already-persisted snapshot, then emits the matching realtime events.

    ``applications`` must be the *rescoped* objects `persist_snapshot`
    returned -- this function has no idea what a host is and trusts its
    caller entirely on that.
    """

    container_statuses = {
        observation.container_ref.container_id: observation.derived_status for observation in observations
    }
    incident_result = process_transitions_and_incidents(
        repository=repository,
        applications=applications,
        container_statuses=container_statuses,
        occurred_at=tick_at,
    )

    # `process_transitions_and_incidents`'s own transaction has already
    # committed by the time it returns above -- every event built from
    # `incident_result` here describes real, persisted state.
    emit_incident_processing_events(repository, result=incident_result, now=tick_at)

    return incident_result


def persist_snapshot_and_process_incidents(
    repository: Repository,
    *,
    host_id: int,
    host_key: str,
    applications: Sequence[Application],
    observations: Sequence[Observation],
    tick_at: datetime,
) -> tuple[PersistDiscoveryReport, IncidentProcessingResult]:
    """`persist_snapshot` immediately followed by
    `process_incidents_for_snapshot` -- what every caller *without* a
    step that needs to happen in between (i.e. everything except
    ``CollectorLoop``, which interleaves its own live-Docker evidence
    collection) actually wants. See ``argus.api.routes.agents``.
    """

    persist_report, scoped_applications = persist_snapshot(
        repository, host_id=host_id, host_key=host_key, applications=applications, observations=observations
    )
    incident_result = process_incidents_for_snapshot(
        repository, applications=scoped_applications, observations=observations, tick_at=tick_at
    )
    return persist_report, incident_result
