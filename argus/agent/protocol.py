"""The Milestone 16 agent <-> control-plane wire contract: plain JSON,
never pickle, never any other arbitrary-object deserialization -- see
the milestone's own "Do not rely on Python pickle" requirement.

Both ``argus.agent`` (which builds and POSTs an ``AgentSnapshot``) and
``argus.api.routes.agents`` (which parses and validates one) import
this module -- it is the one place the shape and the bounds are
defined, so the two sides can never quietly drift apart.

``Application``/``Observation`` reuse their own existing
``to_dict()``/``from_dict()`` (``argus.domain.models``) for the
``applications``/``observations`` fields -- no second, parallel
serialization of the same domain types. ``EvidenceCandidateWire`` is
this module's own small addition: ``argus.evidence.aggregator
.SignalCandidate`` has no ``to_dict``/``from_dict`` of its own (nothing
before this milestone ever needed to put one on the wire), and it also
lacks the ``application_key``/``container_id``/``source_type``/
``source_ref`` fields a control-plane consumer needs to know *where*
to persist it -- see ``argus.evidence.persistence.persist_candidates``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from argus.domain.models import Application, EvidenceCategory, EvidenceSeverity, Observation
from argus.evidence.aggregator import SignalCandidate

__all__ = [
    "PROTOCOL_VERSION",
    "MAX_APPLICATIONS_PER_SNAPSHOT",
    "MAX_OBSERVATIONS_PER_SNAPSHOT",
    "MAX_EVIDENCE_ITEMS_PER_SNAPSHOT",
    "MAX_SAMPLE_LENGTH",
    "MAX_REQUEST_BYTES",
    "MAX_CLOCK_SKEW_SECONDS",
    "ProtocolError",
    "EvidenceCandidateWire",
    "AgentSnapshot",
]

#: Bumped only on a genuine, incompatible wire-shape change -- an
#: ingest request with any other value is rejected outright (see
#: ``argus.api.routes.agents``), never guessed at or best-effort
#: parsed. Milestone 16 starts here, per the spec's own "Start:
#: protocol_version = 1".
PROTOCOL_VERSION = 1

# Milestone 16's own "Payload Limits" requirement: a compromised or
# buggy agent must never be able to send unbounded data into SQLite.
# These are deliberately generous for one real homelab machine's worth
# of Docker Compose stacks, not for a datacenter fleet.
MAX_APPLICATIONS_PER_SNAPSHOT = 200
MAX_OBSERVATIONS_PER_SNAPSHOT = 1000
MAX_EVIDENCE_ITEMS_PER_SNAPSHOT = 500
MAX_SAMPLE_LENGTH = 500
#: A whole ingest request body, in bytes -- enforced by
#: ``argus.api.routes.agents`` before the body is even JSON-decoded.
MAX_REQUEST_BYTES = 2_000_000
#: See the milestone's own "Clock Skew" section. A snapshot whose
#: ``generated_at`` is further than this from the control plane's own
#: current UTC (in either direction) is rejected, not silently
#: rewritten.
MAX_CLOCK_SKEW_SECONDS = 120


class ProtocolError(ValueError):
    """A snapshot's wire representation could not be parsed into
    ``AgentSnapshot`` at all (missing/malformed field) -- distinct from
    the *validation* failures ``argus.api.routes.agents`` layers on top
    (payload limits, clock skew, unknown enum values), which only make
    sense to check once a snapshot has successfully parsed into real
    types."""


def _require_str(data: Mapping[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or value.strip() == "":
        raise ProtocolError(f"{field!r} must be a non-empty string")
    return value


def _require_int(data: Mapping[str, Any], field: str) -> int:
    value = data.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError(f"{field!r} must be an integer")
    return value


def _require_datetime(data: Mapping[str, Any], field: str) -> datetime:
    raw = data.get(field)
    if not isinstance(raw, str):
        raise ProtocolError(f"{field!r} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ProtocolError(f"{field!r} is not a valid ISO-8601 datetime: {raw!r}") from exc
    if parsed.tzinfo is None:
        raise ProtocolError(f"{field!r} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class EvidenceCandidateWire:
    """One bounded, already-redacted evidence item, plus the routing
    fields (``application_key``/``container_id``) a control-plane
    consumer needs to persist it via
    ``argus.evidence.persistence.persist_candidates``.

    ``application_key`` here is always the agent's own *local*,
    unscoped key (e.g. ``"cnstrct"``, never ``"dell:cnstrct"``) --
    exactly like every application key inside this same snapshot's own
    ``applications``/``observations`` -- host-scoping is applied
    exactly once, centrally, by ``argus.ingestion.pipeline`` /
    ``argus.api.routes.agents``, never by the agent itself.
    """

    application_key: str
    container_id: str
    category: EvidenceCategory
    severity: EvidenceSeverity
    normalized_signature: str
    first_seen_at: datetime
    last_seen_at: datetime
    count: int
    sample: str
    source_type: str
    source_ref: str

    def to_signal_candidate(self) -> SignalCandidate:
        return SignalCandidate(
            category=self.category,
            severity=self.severity,
            normalized_signature=self.normalized_signature,
            first_seen_at=self.first_seen_at,
            last_seen_at=self.last_seen_at,
            count=self.count,
            sample=self.sample,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_key": self.application_key,
            "container_id": self.container_id,
            "category": self.category.value,
            "severity": self.severity.value,
            "normalized_signature": self.normalized_signature,
            "first_seen_at": self.first_seen_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat(),
            "count": self.count,
            "sample": self.sample,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceCandidateWire":
        try:
            category = EvidenceCategory(data.get("category"))
            severity = EvidenceSeverity(data.get("severity"))
        except ValueError as exc:
            raise ProtocolError(f"unknown evidence category/severity: {exc}") from exc

        count = _require_int(data, "count")
        if count < 1:
            raise ProtocolError("evidence 'count' must be a positive integer")

        source_type = _require_str(data, "source_type")
        if source_type not in ("container_log", "docker_fact"):
            raise ProtocolError(f"unknown evidence source_type: {source_type!r}")

        sample = _require_str(data, "sample")
        if len(sample) > MAX_SAMPLE_LENGTH:
            raise ProtocolError(f"evidence 'sample' exceeds {MAX_SAMPLE_LENGTH} characters")

        return cls(
            application_key=_require_str(data, "application_key"),
            container_id=_require_str(data, "container_id"),
            category=category,
            severity=severity,
            normalized_signature=_require_str(data, "normalized_signature"),
            first_seen_at=_require_datetime(data, "first_seen_at"),
            last_seen_at=_require_datetime(data, "last_seen_at"),
            count=count,
            sample=sample,
            source_type=source_type,
            source_ref=_require_str(data, "source_ref"),
        )


@dataclass(frozen=True, slots=True)
class AgentSnapshot:
    """One agent poll's worth of sanitized facts -- the entire body of
    one ``POST /api/v1/agents/ingest`` request.

    Carries no credential of any kind -- authentication is the
    ``Authorization: Bearer <token>`` header, entirely outside this
    body (see ``argus.agent.client``/``argus.api.routes.agents``).
    """

    protocol_version: int
    agent_id: str
    host_key: str
    generated_at: datetime
    agent_version: str
    applications: tuple[Application, ...]
    observations: tuple[Observation, ...]
    evidence_candidates: tuple[EvidenceCandidateWire, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "agent_id": self.agent_id,
            "host_key": self.host_key,
            "generated_at": self.generated_at.isoformat(),
            "agent_version": self.agent_version,
            "applications": [app.to_dict() for app in self.applications],
            "observations": [obs.to_dict() for obs in self.observations],
            "evidence_candidates": [ev.to_dict() for ev in self.evidence_candidates],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentSnapshot":
        if not isinstance(data, Mapping):
            raise ProtocolError("request body must be a JSON object")

        protocol_version = data.get("protocol_version")
        if not isinstance(protocol_version, int) or isinstance(protocol_version, bool):
            raise ProtocolError("'protocol_version' must be an integer")

        applications_raw = data.get("applications")
        observations_raw = data.get("observations")
        evidence_raw = data.get("evidence_candidates", [])
        if not isinstance(applications_raw, list) or not isinstance(observations_raw, list):
            raise ProtocolError("'applications' and 'observations' must be lists")
        if not isinstance(evidence_raw, list):
            raise ProtocolError("'evidence_candidates' must be a list")

        try:
            applications = tuple(Application.from_dict(item) for item in applications_raw)
            observations = tuple(Observation.from_dict(item) for item in observations_raw)
        except (KeyError, ValueError, TypeError) as exc:
            raise ProtocolError(f"malformed applications/observations: {exc}") from exc

        evidence_candidates = tuple(EvidenceCandidateWire.from_dict(item) for item in evidence_raw)

        return cls(
            protocol_version=protocol_version,
            agent_id=_require_str(data, "agent_id"),
            host_key=_require_str(data, "host_key"),
            generated_at=_require_datetime(data, "generated_at"),
            agent_version=_require_str(data, "agent_version"),
            applications=applications,
            observations=observations,
            evidence_candidates=evidence_candidates,
        )
