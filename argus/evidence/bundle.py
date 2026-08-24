"""The Milestone 11 evidence bundle -- an immutable, JSON-serializable,
citation-friendly structure. This module only defines the *shape*;
``argus.evidence.assembler`` is what builds one from persisted state.

Every fact carried in a bundle has a stable ``reference`` string of the
form ``"<source_type>:<database_id>"`` (e.g. ``"log_signal:42"``,
``"health_transition:18"``, ``"observation:829"``, ``"incident:14"``) --
this is the whole citation contract: a future model can say "Evidence:
[log_signal:42]" and that string is traceable back to one specific,
already-persisted database row. Nothing here ever asserts *why*
something happened -- see ``TimelineEntry.facts``, which is always a
short, structurally-derived description (a category name, a count, a
from/to status pair), never invented prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

__all__ = [
    "IncidentSummary",
    "ContainerIdentity",
    "ServiceIdentity",
    "ApplicationSummary",
    "EvidenceWindow",
    "SignalItem",
    "TransitionItem",
    "ObservationItem",
    "TimelineEntry",
    "BundleMetadata",
    "EvidenceBundle",
]


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


@dataclass(frozen=True, slots=True)
class IncidentSummary:
    reference: str
    incident_id: int
    status: str
    opened_at: datetime
    closed_at: Optional[datetime]
    opening_status: str
    worst_status: str
    failure_signature: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "incident_id": self.incident_id,
            "status": self.status,
            "opened_at": _iso(self.opened_at),
            "closed_at": _iso(self.closed_at),
            "opening_status": self.opening_status,
            "worst_status": self.worst_status,
            "failure_signature": self.failure_signature,
        }


@dataclass(frozen=True, slots=True)
class ContainerIdentity:
    container_id: str
    name: str
    image: str

    def to_dict(self) -> dict[str, Any]:
        return {"container_id": self.container_id, "name": self.name, "image": self.image}


@dataclass(frozen=True, slots=True)
class ServiceIdentity:
    id: int
    compose_service: Optional[str]
    name: str
    containers: tuple[ContainerIdentity, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "compose_service": self.compose_service,
            "name": self.name,
            "containers": [c.to_dict() for c in self.containers],
        }


@dataclass(frozen=True, slots=True)
class ApplicationSummary:
    key: str
    name: str
    services: tuple[ServiceIdentity, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "services": [s.to_dict() for s in self.services]}


@dataclass(frozen=True, slots=True)
class EvidenceWindow:
    start: datetime
    end: datetime
    incident_open: bool

    def to_dict(self) -> dict[str, Any]:
        return {"start": _iso(self.start), "end": _iso(self.end), "incident_open": self.incident_open}


@dataclass(frozen=True, slots=True)
class SignalItem:
    reference: str
    source_id: int
    category: str
    severity: str
    count: int
    first_seen_at: datetime
    last_seen_at: datetime
    sample: str
    source_type: str
    source_ref: str
    container_id: str
    source_label: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "source_id": self.source_id,
            "category": self.category,
            "severity": self.severity,
            "count": self.count,
            "first_seen_at": _iso(self.first_seen_at),
            "last_seen_at": _iso(self.last_seen_at),
            "sample": self.sample,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "container_id": self.container_id,
            "source_label": self.source_label,
        }


@dataclass(frozen=True, slots=True)
class TransitionItem:
    reference: str
    source_id: int
    scope: str
    label: str
    from_status: Optional[str]
    to_status: str
    occurred_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "source_id": self.source_id,
            "scope": self.scope,
            "label": self.label,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "occurred_at": _iso(self.occurred_at),
        }


@dataclass(frozen=True, slots=True)
class ObservationItem:
    """One sampled observation. ``sampling_reason`` is always one of
    ``"before_transition"`` / ``"at_transition"`` / ``"after_transition"``
    -- structured, not prose -- and ``related_transition_reference``
    names exactly which transition it was sampled around (e.g.
    ``"health_transition:18"``), so an observation can never be selected
    independently of the transition that justified including it (see
    ``argus.evidence.assembler.select_bundle_contents``: an observation
    whose related transition didn't survive budgeting is dropped too)."""

    reference: str
    source_id: int
    container_id: str
    source_label: str
    observed_at: datetime
    docker_state: str
    docker_health: Optional[str]
    restart_count: int
    derived_status: str
    sampling_reason: str
    related_transition_reference: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "source_id": self.source_id,
            "container_id": self.container_id,
            "source_label": self.source_label,
            "observed_at": _iso(self.observed_at),
            "docker_state": self.docker_state,
            "docker_health": self.docker_health,
            "restart_count": self.restart_count,
            "derived_status": self.derived_status,
            "sampling_reason": self.sampling_reason,
            "related_transition_reference": self.related_transition_reference,
        }


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """One unified-timeline row. ``facts`` is always derived mechanically
    from the underlying item's own structured fields -- never a
    sentence invented beyond that (see this module's own docstring)."""

    timestamp: datetime
    reference: str
    entry_type: str
    entity: str
    facts: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": _iso(self.timestamp),
            "reference": self.reference,
            "entry_type": self.entry_type,
            "entity": self.entity,
            "facts": self.facts,
        }


@dataclass(frozen=True, slots=True)
class BundleMetadata:
    generated_at: datetime
    window_start: datetime
    window_end: datetime
    assembler_version: str
    truncated: bool
    omitted_counts: Mapping[str, int]
    evidence_subsystem_status: str
    fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": _iso(self.generated_at),
            "window_start": _iso(self.window_start),
            "window_end": _iso(self.window_end),
            "assembler_version": self.assembler_version,
            "truncated": self.truncated,
            "omitted_counts": dict(self.omitted_counts),
            "evidence_subsystem_status": self.evidence_subsystem_status,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    incident: IncidentSummary
    application: ApplicationSummary
    window: EvidenceWindow
    timeline: tuple[TimelineEntry, ...]
    signals: tuple[SignalItem, ...]
    transitions: tuple[TransitionItem, ...]
    observations: tuple[ObservationItem, ...]
    metadata: BundleMetadata

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident": self.incident.to_dict(),
            "application": self.application.to_dict(),
            "window": self.window.to_dict(),
            "timeline": [entry.to_dict() for entry in self.timeline],
            "signals": [item.to_dict() for item in self.signals],
            "transitions": [item.to_dict() for item in self.transitions],
            "observations": [item.to_dict() for item in self.observations],
            "metadata": self.metadata.to_dict(),
        }

    def to_json(self, *, indent: Optional[int] = 2) -> str:
        import json

        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)
