"""Typed FastAPI response models (DTOs) -- deliberately separate from
`argus.cli.queries`'s read-model dataclasses and `argus.store.repository`'s
persistence records, so neither of those ever needs to change shape
just to satisfy an HTTP response.

Every model here mirrors the *exact* field names/shapes the CLI's own
`--json` output already uses (see each command's `_to_json` function)
-- this is the "CLI parity" the milestone asks for: the API is a second
*transport* for the same read models, never a second *definition* of
what "current application status" or "an incident" means. Every
`from_domain`/`from_record` classmethod below is the one place that
translation happens for its DTO.

Timestamps are always the pre-formatted UTC ISO 8601 string
`argus.cli.formatting.iso` already produces (`None` where absent) --
reused directly rather than left to Pydantic's own datetime
serialization, so API JSON is byte-for-byte identical to the
equivalent CLI `--json` output. Every enum (`HealthStatus`,
`EvidenceCategory`/`EvidenceSeverity`, ...) is read via its own
`.value` before it ever reaches a model -- no Python enum repr ever
reaches a response.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel, ConfigDict

from argus.cli import queries
from argus.cli.formatting import iso
from argus.store.repository import (
    ExplanationRecord,
    IncidentRecord,
    IncidentWithApplicationRecord,
    TransitionHistoryRow,
)

if TYPE_CHECKING:
    # Deliberately deferred to type-checking only: `argus.doctor.checks`
    # pulls in `argus.collectors.docker_client` (and so `docker`) --
    # this module is imported by every route (`argus.api.routes.doctor`
    # included), and must stay free of that import chain at runtime so
    # the architecture guard (`tests/unit/test_api_architecture_guard.py`)
    # can hold "only argus.api.routes.doctor touches Docker" as literally
    # true, not just true for the routes that happen not to import
    # argus.api.models.
    from argus.doctor.checks import DoctorResult

__all__ = [
    "APIErrorDetail",
    "APIErrorEnvelope",
    "CollectorStatusResponse",
    "ApplicationSummaryResponse",
    "SystemStatusResponse",
    "DoctorCheckResponse",
    "DoctorResponse",
    "PortResponse",
    "ContainerDetailResponse",
    "ServiceDetailResponse",
    "OpenIncidentBriefResponse",
    "ApplicationDetailResponse",
    "TransitionResponse",
    "ApplicationHistoryResponse",
    "IncidentResponse",
    "IncidentsListResponse",
    "IncidentDetailResponse",
    "EvidenceItemResponse",
    "EvidenceResponse",
    "BundleContainerResponse",
    "BundleServiceResponse",
    "BundleApplicationResponse",
    "BundleIncidentResponse",
    "BundleWindowResponse",
    "BundleSignalResponse",
    "BundleTransitionResponse",
    "BundleObservationResponse",
    "BundleTimelineEntryResponse",
    "BundleMetadataResponse",
    "EvidenceBundleResponse",
    "ExplanationClaimResponse",
    "RecommendationResponse",
    "ExplanationBodyResponse",
    "UsageResponse",
    "ExplanationResponse",
    "ExplanationsListResponse",
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Errors (documented in the OpenAPI schema only -- actual responses are
# built by argus.api.errors.register_exception_handlers)
# --------------------------------------------------------------------------


class APIErrorDetail(_Model):
    code: str
    message: str


class APIErrorEnvelope(_Model):
    error: APIErrorDetail


# --------------------------------------------------------------------------
# System status
# --------------------------------------------------------------------------


class CollectorStatusResponse(_Model):
    status: str
    last_tick_at: Optional[str]
    last_success_at: Optional[str]
    consecutive_failures: int
    last_error: Optional[str]

    @classmethod
    def from_domain(cls, status: "queries.CollectorStatusView") -> "CollectorStatusResponse":
        return cls(
            status=status.classification,
            last_tick_at=iso(status.last_tick_at),
            last_success_at=iso(status.last_success_at),
            consecutive_failures=status.consecutive_failures,
            last_error=status.last_error,
        )


class ApplicationSummaryResponse(_Model):
    key: str
    name: str
    status: str
    services: int
    containers: int
    last_seen_at: Optional[str]

    @classmethod
    def from_domain(cls, summary: "queries.ApplicationSummary") -> "ApplicationSummaryResponse":
        return cls(
            key=summary.key,
            name=summary.name,
            status=summary.status.value,
            services=summary.service_count,
            containers=summary.container_count,
            last_seen_at=iso(summary.last_seen_at),
        )


class SystemStatusResponse(_Model):
    collector: CollectorStatusResponse
    applications: list[ApplicationSummaryResponse]
    open_incidents: int


# --------------------------------------------------------------------------
# Doctor (argus.api.routes.doctor only)
# --------------------------------------------------------------------------


class DoctorCheckResponse(_Model):
    name: str
    status: str
    message: Optional[str]


class DoctorResponse(_Model):
    operational: bool
    checks: list[DoctorCheckResponse]

    @classmethod
    def from_domain(cls, result: DoctorResult) -> "DoctorResponse":
        return cls(
            operational=result.operational,
            checks=[
                DoctorCheckResponse(name=check.name, status=check.status.value, message=check.message)
                for check in result.checks
            ],
        )


# --------------------------------------------------------------------------
# Applications: list + detail + history
# --------------------------------------------------------------------------


class PortResponse(_Model):
    container_port: int
    protocol: str
    host_binding: Optional[str]

    @classmethod
    def from_domain(cls, port: "queries.PortView") -> "PortResponse":
        return cls(container_port=port.container_port, protocol=port.protocol, host_binding=port.host_binding)


class ContainerDetailResponse(_Model):
    name: str
    docker_state: str
    docker_health: Optional[str]
    restart_count: int
    ports: list[PortResponse]

    @classmethod
    def from_domain(cls, container: "queries.ContainerDetail") -> "ContainerDetailResponse":
        return cls(
            name=container.name,
            docker_state=container.docker_state,
            docker_health=container.docker_health,
            restart_count=container.restart_count,
            ports=[PortResponse.from_domain(port) for port in container.ports],
        )


class ServiceDetailResponse(_Model):
    compose_service: Optional[str]
    name: str
    status: str
    container: Optional[ContainerDetailResponse]

    @classmethod
    def from_domain(cls, service: "queries.ServiceDetail") -> "ServiceDetailResponse":
        return cls(
            compose_service=service.compose_service,
            name=service.display_name,
            status=service.status.value,
            container=ContainerDetailResponse.from_domain(service.container) if service.container is not None else None,
        )


class OpenIncidentBriefResponse(_Model):
    id: int
    status: str
    opened_at: Optional[str]
    closed_at: Optional[str]
    opening_status: str
    worst_status: str

    @classmethod
    def from_domain(cls, incident: "IncidentWithApplicationRecord") -> "OpenIncidentBriefResponse":
        return cls(
            id=incident.id,
            status=incident.status,
            opened_at=iso(incident.opened_at),
            closed_at=iso(incident.closed_at),
            opening_status=incident.opening_status.value,
            worst_status=incident.worst_status.value,
        )


class ApplicationDetailResponse(_Model):
    key: str
    name: str
    status: str
    last_seen_at: Optional[str]
    services: list[ServiceDetailResponse]
    open_incident: Optional[OpenIncidentBriefResponse]

    @classmethod
    def from_domain(cls, detail: "queries.ApplicationDetail") -> "ApplicationDetailResponse":
        return cls(
            key=detail.key,
            name=detail.name,
            status=detail.status.value,
            last_seen_at=iso(detail.last_seen_at),
            services=[ServiceDetailResponse.from_domain(service) for service in detail.services],
            open_incident=(
                OpenIncidentBriefResponse.from_domain(detail.open_incident)
                if detail.open_incident is not None
                else None
            ),
        )


class TransitionResponse(_Model):
    occurred_at: Optional[str]
    scope: str
    label: str
    from_status: Optional[str]
    to_status: str

    @classmethod
    def from_domain(cls, row: "TransitionHistoryRow") -> "TransitionResponse":
        return cls(
            occurred_at=iso(row.occurred_at),
            scope=row.scope,
            label=row.label,
            from_status=row.from_status.value if row.from_status is not None else None,
            to_status=row.to_status.value,
        )


class ApplicationHistoryResponse(_Model):
    application: str
    since: Optional[str]
    transitions: list[TransitionResponse]


# --------------------------------------------------------------------------
# Incidents
# --------------------------------------------------------------------------


class IncidentResponse(_Model):
    id: int
    application: str
    application_key: str
    status: str
    opened_at: Optional[str]
    closed_at: Optional[str]
    opening_status: str
    worst_status: str
    failure_signature: str

    @classmethod
    def from_domain(cls, incident: "IncidentWithApplicationRecord") -> "IncidentResponse":
        return cls(
            id=incident.id,
            application=incident.application_name,
            application_key=incident.application_key,
            status=incident.status,
            opened_at=iso(incident.opened_at),
            closed_at=iso(incident.closed_at),
            opening_status=incident.opening_status.value,
            worst_status=incident.worst_status.value,
            failure_signature=incident.failure_signature,
        )


class IncidentsListResponse(_Model):
    incidents: list[IncidentResponse]


class IncidentDetailResponse(_Model):
    id: int
    application_key: str
    application_name: str
    failure_signature: str
    status: str
    opening_status: str
    worst_status: str
    opened_at: Optional[str]
    closed_at: Optional[str]
    evidence_count: int
    explanation_count: int
    has_cached_explanation: bool

    @classmethod
    def from_domain(
        cls,
        incident: "IncidentRecord",
        *,
        application_key: str,
        application_name: str,
        evidence_count: int,
        explanation_count: int,
    ) -> "IncidentDetailResponse":
        return cls(
            id=incident.id,
            application_key=application_key,
            application_name=application_name,
            failure_signature=incident.failure_signature,
            status=incident.status,
            opening_status=incident.opening_status.value,
            worst_status=incident.worst_status.value,
            opened_at=iso(incident.opened_at),
            closed_at=iso(incident.closed_at),
            evidence_count=evidence_count,
            explanation_count=explanation_count,
            has_cached_explanation=explanation_count > 0,
        )


# --------------------------------------------------------------------------
# Evidence -- only ever redacted, already-persisted samples (see
# argus.evidence.redaction: nothing unredacted is ever stored, so
# nothing unredacted can ever be returned here).
# --------------------------------------------------------------------------


class EvidenceItemResponse(_Model):
    category: str
    severity: str
    count: int
    first_seen_at: Optional[str]
    last_seen_at: Optional[str]
    sample: str
    source: str
    source_type: str

    @classmethod
    def from_domain(cls, view: "queries.EvidenceView") -> "EvidenceItemResponse":
        return cls(
            category=view.category.value,
            severity=view.severity.value,
            count=view.count,
            first_seen_at=iso(view.first_seen_at),
            last_seen_at=iso(view.last_seen_at),
            sample=view.sample,
            source=view.source_label,
            source_type=view.source_type,
        )


class EvidenceResponse(_Model):
    incident_id: int
    evidence: list[EvidenceItemResponse]


# --------------------------------------------------------------------------
# Evidence bundle -- mirrors argus.evidence.bundle.EvidenceBundle.to_dict()
# exactly; constructed via `EvidenceBundleResponse.model_validate(bundle.to_dict())`
# rather than a from_domain, since that dict is already the same
# already-ISO-stringified, JSON-safe shape `argus bundle --json` prints.
# --------------------------------------------------------------------------


class BundleContainerResponse(_Model):
    container_id: str
    name: str
    image: str


class BundleServiceResponse(_Model):
    id: int
    compose_service: Optional[str]
    name: str
    containers: list[BundleContainerResponse]


class BundleApplicationResponse(_Model):
    key: str
    name: str
    services: list[BundleServiceResponse]


class BundleIncidentResponse(_Model):
    reference: str
    incident_id: int
    status: str
    opened_at: Optional[str]
    closed_at: Optional[str]
    opening_status: str
    worst_status: str
    failure_signature: str


class BundleWindowResponse(_Model):
    start: Optional[str]
    end: Optional[str]
    incident_open: bool


class BundleSignalResponse(_Model):
    reference: str
    source_id: int
    category: str
    severity: str
    count: int
    first_seen_at: Optional[str]
    last_seen_at: Optional[str]
    sample: str
    source_type: str
    source_ref: str
    container_id: str
    source_label: str


class BundleTransitionResponse(_Model):
    reference: str
    source_id: int
    scope: str
    label: str
    from_status: Optional[str]
    to_status: str
    occurred_at: Optional[str]


class BundleObservationResponse(_Model):
    reference: str
    source_id: int
    container_id: str
    source_label: str
    observed_at: Optional[str]
    docker_state: str
    docker_health: Optional[str]
    restart_count: int
    derived_status: str
    sampling_reason: str
    related_transition_reference: str


class BundleTimelineEntryResponse(_Model):
    timestamp: Optional[str]
    reference: str
    entry_type: str
    entity: str
    facts: str


class BundleMetadataResponse(_Model):
    generated_at: Optional[str]
    window_start: Optional[str]
    window_end: Optional[str]
    assembler_version: str
    truncated: bool
    omitted_counts: dict[str, int]
    evidence_subsystem_status: str
    fingerprint: str


class EvidenceBundleResponse(_Model):
    incident: BundleIncidentResponse
    application: BundleApplicationResponse
    window: BundleWindowResponse
    timeline: list[BundleTimelineEntryResponse]
    signals: list[BundleSignalResponse]
    transitions: list[BundleTransitionResponse]
    observations: list[BundleObservationResponse]
    metadata: BundleMetadataResponse


# --------------------------------------------------------------------------
# Explanations -- persisted, validated records only. Built straight from
# `ExplanationRecord.response_json` (itself already
# `IncidentExplanation.to_dict()`, per argus.ai.explain._dump_response_json)
# via plain `json.loads` -- this module never imports `argus.ai` at all,
# so there is no path by which reading a persisted explanation could
# instantiate an AI provider.
# --------------------------------------------------------------------------


class ExplanationClaimResponse(_Model):
    text: str
    evidence_references: list[str]


class RecommendationResponse(_Model):
    category: str
    explanation: Optional[str]


class ExplanationBodyResponse(_Model):
    incident_id: int
    summary: str
    root_cause_claim: Optional[ExplanationClaimResponse]
    supporting_claims: list[ExplanationClaimResponse]
    confidence: str
    recommendation: Optional[RecommendationResponse]
    caveats: list[str]


class UsageResponse(_Model):
    input_tokens: Optional[int]
    output_tokens: Optional[int]


class ExplanationResponse(_Model):
    id: int
    incident_id: int
    provider: str
    model: str
    prompt_version: str
    bundle_fingerprint: str
    created_at: Optional[str]
    usage: Optional[UsageResponse]
    explanation: ExplanationBodyResponse

    @classmethod
    def from_record(cls, record: ExplanationRecord) -> "ExplanationResponse":
        import json

        body = json.loads(record.response_json)
        usage = (
            UsageResponse(input_tokens=record.input_tokens, output_tokens=record.output_tokens)
            if record.input_tokens is not None or record.output_tokens is not None
            else None
        )
        return cls(
            id=record.id,
            incident_id=record.incident_id,
            provider=record.provider,
            model=record.model,
            prompt_version=record.prompt_version,
            bundle_fingerprint=record.bundle_fingerprint,
            created_at=iso(record.created_at),
            usage=usage,
            explanation=ExplanationBodyResponse.model_validate(body),
        )


class ExplanationsListResponse(_Model):
    incident_id: int
    explanations: list[ExplanationResponse]
