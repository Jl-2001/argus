"""The Milestone 11 evidence assembler.

Given one persisted incident, deterministically builds a compact,
bounded, citation-friendly ``EvidenceBundle`` (see
``argus.evidence.bundle``) suitable for a future LLM reasoning call --
without ever calling Docker, without ever calling a model, and without
ever inventing a fact that isn't already a persisted database row.

Two layers, kept deliberately separate:

* ``select_bundle_contents`` -- a **pure** selection/budgeting function.
  Given already-fetched, already-labeled candidate items, it decides
  priority ordering and which ones survive the configured item-count
  budgets. No SQLite, no Docker, no clock reads -- fully testable with
  hand-built in-memory candidates (see
  ``tests/unit/test_evidence_assembler_selection.py``).
* ``assemble_evidence_bundle`` -- the DB-facing orchestrator. Fetches
  candidates via ``argus.store.repository.Repository`` (never raw SQL
  here), hands them to the pure selector, fits the result within the
  character budget, computes the fingerprint, and returns the final
  ``EvidenceBundle``.

The future AI layer's whole contract with this module is exactly one
call: ``assemble_evidence_bundle(repository, incident_id, now=...)``. It
never needs to query SQLite, read Docker, read raw logs, decide which
rows matter, or redact secrets -- all of that already happened by the
time this function returns.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from argus.domain.models import EVIDENCE_SEVERITY_RANK, EvidenceRecord
from argus.evidence.bundle import (
    ApplicationSummary,
    BundleMetadata,
    ContainerIdentity,
    EvidenceBundle,
    EvidenceWindow,
    IncidentSummary,
    ObservationItem,
    ServiceIdentity,
    SignalItem,
    TimelineEntry,
    TransitionItem,
)
from argus.store.repository import Repository, TransitionHistoryRow

__all__ = [
    "ASSEMBLER_VERSION",
    "IncidentNotFoundError",
    "AssemblerConfig",
    "DEFAULT_ASSEMBLER_CONFIG",
    "SelectionResult",
    "select_bundle_contents",
    "assemble_evidence_bundle",
]

logger = logging.getLogger(__name__)

ASSEMBLER_VERSION = "1"

#: Coarser scope reflects the incident itself more directly than a
#: single container does -- application-scope transitions rank above
#: service-scope, which ranks above container-scope, when the
#: transition budget forces a choice. A plain dict, never declaration
#: order -- same discipline as every other explicit ranking in this
#: codebase (argus.domain.health, argus.incidents.engine,
#: argus.domain.models.EVIDENCE_SEVERITY_RANK).
_TRANSITION_SCOPE_RANK: dict[str, int] = {"application": 0, "service": 1, "container": 2}


class IncidentNotFoundError(RuntimeError):
    """Raised by `assemble_evidence_bundle` when `incident_id` doesn't
    exist -- never a raw KeyError/AttributeError traceback."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssemblerConfig:
    """The assembler's own bounds -- deliberately not a general settings
    subsystem, matching the same "just enough numbers, explicit
    defaults" shape as `argus.collector.loop.CollectorConfig` and
    `argus.evidence.collector.EvidenceCollectionLimits`.
    """

    pre_open_window_seconds: int = 120
    post_close_window_seconds: int = 120
    max_signals: int = 40
    max_transitions: int = 40
    max_observations: int = 30
    max_sample_chars: int = 500
    max_total_chars: int = 20_000

    def __post_init__(self) -> None:
        for name in (
            "pre_open_window_seconds", "post_close_window_seconds", "max_signals", "max_transitions",
            "max_observations", "max_sample_chars", "max_total_chars",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


DEFAULT_ASSEMBLER_CONFIG = AssemblerConfig()


# --------------------------------------------------------------------------
# Pure selection layer
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SelectionResult:
    signals: tuple[SignalItem, ...]
    transitions: tuple[TransitionItem, ...]
    observations: tuple[ObservationItem, ...]
    omitted_counts: dict[str, int]


def _seconds_from_open(timestamp: datetime, opened_at: datetime) -> float:
    return abs((timestamp - opened_at).total_seconds())


def _rank_signals(signals: Sequence[SignalItem], opened_at: datetime) -> list[SignalItem]:
    """Priority: critical > high > warning > info; then nearer to
    incident opening; then higher count; then a deterministic id
    tie-break. Ascending sort key -- most important first."""

    return sorted(
        signals,
        key=lambda s: (
            -EVIDENCE_SEVERITY_RANK[_severity_enum(s.severity)],
            _seconds_from_open(s.first_seen_at, opened_at),
            -s.count,
            s.source_id,
        ),
    )


def _severity_enum(value: str):
    from argus.domain.models import EvidenceSeverity

    return EvidenceSeverity(value)


def _rank_transitions(transitions: Sequence[TransitionItem], opened_at: datetime) -> list[TransitionItem]:
    """Priority: application scope > service scope > container scope
    (the coarser the scope, the more directly it reflects the incident
    itself); then nearer to incident opening; then a deterministic id
    tie-break."""

    return sorted(
        transitions,
        key=lambda t: (
            _TRANSITION_SCOPE_RANK.get(t.scope, 99),
            _seconds_from_open(t.occurred_at, opened_at),
            t.source_id,
        ),
    )


def _rank_observations(observations: Sequence[ObservationItem], opened_at: datetime) -> list[ObservationItem]:
    """No severity concept for a raw observation -- priority is purely
    nearness to incident opening, then a deterministic id tie-break."""

    return sorted(
        observations,
        key=lambda o: (_seconds_from_open(o.observed_at, opened_at), o.source_id),
    )


def select_bundle_contents(
    *,
    signals: Sequence[SignalItem],
    transitions: Sequence[TransitionItem],
    observations: Sequence[ObservationItem],
    config: AssemblerConfig,
    opened_at: datetime,
) -> SelectionResult:
    """Pure priority ranking + item-count budgeting. No SQLite, no
    Docker, no clock reads -- every input is an already-built candidate
    item; every rule here is the documented, testable priority policy
    (see this module's own docstring and the Milestone 11 report).

    Character-budget fitting (which may shrink samples or drop further
    items) is deliberately *not* done here -- that happens in
    `assemble_evidence_bundle`, after this function has already applied
    the "real" priority policy; the character budget is a last-resort
    safety net, not the primary selection mechanism.
    """

    ranked_signals = _rank_signals(signals, opened_at)
    selected_signals = ranked_signals[: config.max_signals]
    omitted_signals = len(ranked_signals) - len(selected_signals)

    ranked_transitions = _rank_transitions(transitions, opened_at)
    selected_transitions = ranked_transitions[: config.max_transitions]
    omitted_transitions = len(ranked_transitions) - len(selected_transitions)

    # An observation sampled around a transition that didn't survive the
    # transition budget above would be a dangling citation (its own
    # `related_transition_reference` would point at nothing in the
    # bundle) -- so it's dropped too, not kept independently.
    selected_transition_refs = {t.reference for t in selected_transitions}
    eligible = [o for o in observations if o.related_transition_reference in selected_transition_refs]

    ranked_observations = _rank_observations(eligible, opened_at)
    deduped: list[ObservationItem] = []
    seen_refs: set[str] = set()
    for observation in ranked_observations:
        if observation.reference in seen_refs:
            continue
        seen_refs.add(observation.reference)
        deduped.append(observation)

    selected_observations = deduped[: config.max_observations]
    # Counted against the *original* candidate count, not just the
    # already-eligible subset -- an observation dropped because its own
    # related transition didn't survive budgeting is just as real an
    # omission (from the bundle consumer's point of view) as one dropped
    # for its own budget reasons; `omitted_counts` exists precisely so
    # the model knows it did not receive everything, and undercounting
    # here would misrepresent that.
    omitted_observations = len(observations) - len(selected_observations)

    return SelectionResult(
        signals=tuple(selected_signals),
        transitions=tuple(selected_transitions),
        observations=tuple(selected_observations),
        omitted_counts={
            "signals": omitted_signals,
            "transitions": omitted_transitions,
            "observations": omitted_observations,
        },
    )


# --------------------------------------------------------------------------
# Timeline construction
# --------------------------------------------------------------------------


def _signal_facts(signal: SignalItem) -> str:
    return f"{signal.category} ×{signal.count}"


def _transition_facts(transition: TransitionItem) -> str:
    from_label = transition.from_status if transition.from_status is not None else "NULL"
    return f"{from_label} -> {transition.to_status}"


def _observation_facts(observation: ObservationItem) -> str:
    health = f", docker_health={observation.docker_health}" if observation.docker_health is not None else ""
    return (
        f"docker_state={observation.docker_state}{health}, "
        f"restart_count={observation.restart_count}, status={observation.derived_status}"
    )


def _build_timeline(
    signals: Sequence[SignalItem], transitions: Sequence[TransitionItem], observations: Sequence[ObservationItem]
) -> tuple[TimelineEntry, ...]:
    """One unified, chronological timeline over exactly the bundle's own
    (already-selected) contents -- never over omitted items. Sort:
    timestamp ascending, then entry_type (alphabetical -- which also
    happens to read as "health_transition, log_signal, observation"),
    then source id -- fully deterministic, no reliance on retrieval
    order."""

    entries: list[TimelineEntry] = []
    for signal in signals:
        entries.append(
            TimelineEntry(
                timestamp=signal.first_seen_at, reference=signal.reference, entry_type="log_signal",
                entity=signal.source_label, facts=_signal_facts(signal),
            )
        )
    for transition in transitions:
        entries.append(
            TimelineEntry(
                timestamp=transition.occurred_at, reference=transition.reference, entry_type="health_transition",
                entity=transition.label, facts=_transition_facts(transition),
            )
        )
    for observation in observations:
        entries.append(
            TimelineEntry(
                timestamp=observation.observed_at, reference=observation.reference, entry_type="observation",
                entity=observation.source_label, facts=_observation_facts(observation),
            )
        )

    entries.sort(key=lambda e: (e.timestamp, e.entry_type, e.reference))
    return tuple(entries)


# --------------------------------------------------------------------------
# Character budget
# --------------------------------------------------------------------------

_MIN_SAMPLE_CHARS = 20
_MAX_BUDGET_ITERATIONS = 500


def _fit_within_character_budget(
    *,
    incident: IncidentSummary,
    application: ApplicationSummary,
    window: EvidenceWindow,
    signals: list[SignalItem],
    transitions: list[TransitionItem],
    observations: list[ObservationItem],
    config: AssemblerConfig,
    generated_at: datetime,
    evidence_subsystem_status: str,
    omitted_counts: dict[str, int],
) -> EvidenceBundle:
    """Fits the already-priority-selected contents within
    `config.max_total_chars`, preferring to shrink signal samples before
    dropping any fact, and dropping lowest-priority facts (observations,
    then signals, then transitions -- the reverse of the priority order
    documented in `select_bundle_contents`) only once samples are
    already at the minimum. Never produces invalid JSON -- every
    intermediate state is a real, fully-formed `EvidenceBundle`.
    """

    sample_cap = config.max_sample_chars
    omitted = dict(omitted_counts)

    def build(truncated: bool) -> EvidenceBundle:
        metadata = BundleMetadata(
            generated_at=generated_at, window_start=window.start, window_end=window.end,
            assembler_version=ASSEMBLER_VERSION, truncated=truncated, omitted_counts=dict(omitted),
            evidence_subsystem_status=evidence_subsystem_status, fingerprint="",
        )
        return EvidenceBundle(
            incident=incident, application=application, window=window,
            timeline=_build_timeline(signals, transitions, observations),
            signals=tuple(signals), transitions=tuple(transitions), observations=tuple(observations),
            metadata=metadata,
        )

    truncated = any(count > 0 for count in omitted.values())
    bundle = build(truncated)

    iterations = 0
    while len(bundle.to_json(indent=None)) > config.max_total_chars and iterations < _MAX_BUDGET_ITERATIONS:
        iterations += 1
        if sample_cap > _MIN_SAMPLE_CHARS and any(len(s.sample) > _MIN_SAMPLE_CHARS for s in signals):
            sample_cap = max(_MIN_SAMPLE_CHARS, sample_cap // 2)
            signals = [replace(s, sample=s.sample[:sample_cap]) for s in signals]
            truncated = True
        elif observations:
            observations = observations[:-1]
            omitted["observations"] = omitted.get("observations", 0) + 1
            truncated = True
        elif signals:
            signals = signals[:-1]
            omitted["signals"] = omitted.get("signals", 0) + 1
            truncated = True
        elif transitions:
            transitions = transitions[:-1]
            omitted["transitions"] = omitted.get("transitions", 0) + 1
            truncated = True
        else:
            break  # nothing left to drop -- the bundle is already minimal

        bundle = build(truncated)

    fingerprint = _compute_fingerprint(bundle)
    return replace(bundle, metadata=replace(bundle.metadata, fingerprint=fingerprint))


def _compute_fingerprint(bundle: EvidenceBundle) -> str:
    """SHA-256 over the bundle's canonical *content*, deliberately
    excluding two bookkeeping fields that describe *when this bundle was
    built*, not *what it contains*:

    * `metadata.generated_at` -- always excluded, so the same underlying
      facts fingerprint identically regardless of invocation time (e.g.
      re-assembling a resolved incident's bundle a week later reproduces
      the same hash).
    * `window.end` -- excluded *only when the incident is still open*
      (`window.incident_open`). For an open incident, `window.end`
      equals `now` (see `assemble_evidence_bundle`), so it advances on
      every single call whether or not any new evidence actually
      arrived. Including it would mean an open incident's fingerprint
      never matches twice -- even two calls a second apart with
      genuinely zero new evidence -- which would silently defeat
      Milestone 12's entire caching mechanism for every open incident.
      Any evidence that *actually* becomes newly visible as the window
      grows still changes the fingerprint on its own merits: it shows up
      as a real difference in `signals`/`transitions`/`observations`,
      which remain part of the hash. `window.end` for a *resolved*
      incident is a fixed fact (`closed_at` + the configured post-close
      window), never a moving target, so it always participates.
    """

    payload = bundle.to_dict()
    metadata = dict(payload["metadata"])
    metadata.pop("generated_at", None)
    metadata["fingerprint"] = ""
    if bundle.window.incident_open:
        # `metadata.window_end` duplicates `window.end` (see
        # BundleMetadata/EvidenceWindow) -- both copies must be
        # neutralized together, or this whole exclusion is a no-op.
        metadata["window_end"] = None
    payload["metadata"] = metadata
    if bundle.window.incident_open:
        window = dict(payload["window"])
        window["end"] = None
        payload["window"] = window
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Repository-facing helpers
# --------------------------------------------------------------------------


def _resolve_source_label(repository: Repository, container_id: str) -> str:
    """Duplicated, deliberately, from `argus.cli.queries`'s own private
    helper of the same shape: the CLI package and the evidence package
    are siblings, and importing `argus.cli` from `argus.evidence` would
    invert the dependency direction (the CLI depends on the evidence
    package's read model, never the reverse). A five-line resolver is
    cheaper to duplicate than to refactor into a shared home this
    milestone doesn't otherwise need.
    """

    container_record = repository.get_container_by_docker_id(container_id)
    if container_record is None:
        return container_id
    service_record = repository.get_service(container_record.service_id)
    if service_record is not None and service_record.compose_service is not None:
        return service_record.compose_service
    return container_record.name


def _build_signal_item(repository: Repository, evidence: EvidenceRecord, *, max_sample_chars: int) -> SignalItem:
    return SignalItem(
        reference=f"log_signal:{evidence.id}",
        source_id=evidence.id,
        category=evidence.category.value,
        severity=evidence.severity.value,
        count=evidence.count,
        first_seen_at=evidence.first_seen_at,
        last_seen_at=evidence.last_seen_at,
        # `max_sample_chars` is enforced here, unconditionally -- not
        # only as a side effect of `_fit_within_character_budget` running
        # out of room. A signal whose sample is 2000 chars must never
        # reach the bundle un-truncated just because the *total* bundle
        # happens to be well under `max_total_chars`.
        sample=evidence.sample[:max_sample_chars],
        source_type=evidence.source_type,
        source_ref=evidence.source_ref,
        container_id=evidence.container_id,
        source_label=_resolve_source_label(repository, evidence.container_id),
    )


def _build_transition_item(row: TransitionHistoryRow) -> TransitionItem:
    return TransitionItem(
        reference=f"health_transition:{row.id}",
        source_id=row.id,
        scope=row.scope,
        label=row.label,
        from_status=row.from_status.value if row.from_status is not None else None,
        to_status=row.to_status.value,
        occurred_at=row.occurred_at,
    )


def _sample_observations_for_transition(
    repository: Repository, transition_item: TransitionItem, container_docker_id: str
) -> list[ObservationItem]:
    """"One before, the transition's own, one after" -- a deterministic,
    bounded sampling strategy, never every 15-second poll in the
    window."""

    items: list[ObservationItem] = []

    before = repository.get_observation_before(container_docker_id, before=transition_item.occurred_at)
    if before is not None:
        items.append(_observation_record_to_item(before, transition_item, "before_transition"))

    at = repository.get_observation_at(container_docker_id, at=transition_item.occurred_at)
    if at is not None:
        items.append(_observation_record_to_item(at, transition_item, "at_transition"))

    after = repository.get_observation_after(container_docker_id, after=transition_item.occurred_at)
    if after is not None:
        items.append(_observation_record_to_item(after, transition_item, "after_transition"))

    return items


def _observation_record_to_item(record, transition_item: TransitionItem, reason: str) -> ObservationItem:
    observation = record.observation
    return ObservationItem(
        reference=f"observation:{record.id}",
        source_id=record.id,
        container_id=observation.container_ref.container_id,
        source_label=transition_item.label,
        observed_at=observation.observed_at,
        docker_state=observation.docker_state.value,
        docker_health=observation.docker_health.value if observation.docker_health is not None else None,
        restart_count=observation.restart_count,
        derived_status=observation.derived_status.value,
        sampling_reason=reason,
        related_transition_reference=transition_item.reference,
    )


def _evidence_subsystem_status(repository: Repository) -> str:
    state = repository.get_collector_state()
    if state.last_evidence_success_at is None and state.consecutive_evidence_failures == 0:
        return "never_run"
    if state.consecutive_evidence_failures > 0:
        return "degraded"
    return "healthy"


# --------------------------------------------------------------------------
# Public entrypoint
# --------------------------------------------------------------------------


def assemble_evidence_bundle(
    repository: Repository,
    incident_id: int,
    *,
    now: datetime,
    config: AssemblerConfig = DEFAULT_ASSEMBLER_CONFIG,
) -> EvidenceBundle:
    """Deterministically build the evidence bundle for one persisted
    incident. Read-only: never calls Docker, never calls a model, never
    writes to the database. Raises `IncidentNotFoundError` if
    `incident_id` doesn't exist -- never a raw `KeyError`.
    """

    incident = repository.get_incident_by_id(incident_id)
    if incident is None:
        raise IncidentNotFoundError(f"incident {incident_id} does not exist")

    application_record = repository.get_application_by_id(incident.scope_id)
    if application_record is None:
        raise IncidentNotFoundError(
            f"incident {incident_id} references application row {incident.scope_id}, "
            "which no longer exists -- the database is in an inconsistent state"
        )

    incident_open = incident.status == "open"
    window_start = incident.opened_at - timedelta(seconds=config.pre_open_window_seconds)
    if incident_open or incident.closed_at is None:
        window_end = now
    else:
        window_end = incident.closed_at + timedelta(seconds=config.post_close_window_seconds)

    window = EvidenceWindow(start=window_start, end=window_end, incident_open=incident_open)

    # -- Application identity context (never budgeted/truncated -- small
    # and bounded by design already) --
    services: list[ServiceIdentity] = []
    for service_record in repository.get_services_for_application(application_record.id):
        containers = tuple(
            ContainerIdentity(container_id=c.container_id, name=c.name, image=_container_image(repository, c))
            for c in repository.get_containers_for_service(service_record.id)
        )
        services.append(
            ServiceIdentity(
                id=service_record.id,
                compose_service=service_record.compose_service,
                name=service_record.compose_service or service_record.name,
                containers=containers,
            )
        )
    application = ApplicationSummary(key=application_record.key, name=application_record.name, services=tuple(services))

    incident_summary = IncidentSummary(
        reference=f"incident:{incident.id}",
        incident_id=incident.id,
        status=incident.status,
        opened_at=incident.opened_at,
        closed_at=incident.closed_at,
        opening_status=incident.opening_status.value,
        worst_status=incident.worst_status.value,
        failure_signature=incident.failure_signature,
    )

    # -- Candidate signals: the incident's own linked evidence is the
    # primary source, per the Milestone 11 specification --
    linked_evidence = repository.list_evidence_for_incident(incident_id)
    candidate_signals = [
        _build_signal_item(repository, evidence, max_sample_chars=config.max_sample_chars)
        for evidence in linked_evidence
    ]

    # -- Candidate transitions: bounded to the incident's own context
    # window, never "all history" --
    transition_rows = repository.get_transitions_in_window(
        application_record.id, window_start=window_start, window_end=window_end
    )
    candidate_transitions = [_build_transition_item(row) for row in transition_rows]

    # -- Candidate observations: sampled around each container-scope
    # transition only (application/service-scope transitions have no
    # single Observation of their own) --
    candidate_observations: list[ObservationItem] = []
    for row, transition_item in zip(transition_rows, candidate_transitions):
        if row.container_docker_id is None:
            continue
        candidate_observations.extend(
            _sample_observations_for_transition(repository, transition_item, row.container_docker_id)
        )

    selection = select_bundle_contents(
        signals=candidate_signals, transitions=candidate_transitions, observations=candidate_observations,
        config=config, opened_at=incident.opened_at,
    )

    evidence_subsystem_status = _evidence_subsystem_status(repository)

    return _fit_within_character_budget(
        incident=incident_summary, application=application, window=window,
        signals=list(selection.signals), transitions=list(selection.transitions),
        observations=list(selection.observations), config=config, generated_at=now,
        evidence_subsystem_status=evidence_subsystem_status, omitted_counts=selection.omitted_counts,
    )


def _container_image(repository: Repository, container_record) -> str:
    """The container identity table itself doesn't carry `image` (only
    observations do -- see argus.store.schema.sql) -- resolved here from
    the latest observation, falling back to a placeholder if this
    container has no observation history at all yet."""

    observation = repository.get_latest_observation(container_record.container_id)
    return observation.container_ref.image if observation is not None else "unknown"
