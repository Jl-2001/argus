"""GET /api/v1/incidents, /{incident_id} -- incident history and detail,
built from `argus.cli.queries.list_incidents` (the same read model
`argus incidents` already uses) plus a couple of small persisted
counts (`Repository.list_evidence_for_incident`,
`Repository.list_explanations_for_incident`) for the detail view. Never
triggers AI generation -- both counts and "has_cached_explanation" only
ever report what is already persisted.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from argus.api.dependencies import get_repository
from argus.api.errors import incident_not_found, invalid_query_parameter
from argus.api.models import IncidentDetailResponse, IncidentResponse, IncidentsListResponse
from argus.cli import queries
from argus.store.repository import Repository

router = APIRouter()

_VALID_STATUS_FILTERS = {"open", "all"}


@router.get("", response_model=IncidentsListResponse, summary="Incident history, newest opened first")
def list_incidents(
    status: Optional[str] = Query(None, description="'open' for open incidents only; omit (or 'all') for all"),
    repository: Repository = Depends(get_repository),
) -> IncidentsListResponse:
    if status is not None and status.strip().lower() not in _VALID_STATUS_FILTERS:
        raise invalid_query_parameter(f"status {status!r} must be one of: {', '.join(sorted(_VALID_STATUS_FILTERS))}")

    open_only = status is not None and status.strip().lower() == "open"
    incidents = queries.list_incidents(repository, open_only=open_only)
    return IncidentsListResponse(incidents=[IncidentResponse.from_domain(incident) for incident in incidents])


@router.get("/{incident_id}", response_model=IncidentDetailResponse, summary="One incident's persisted state")
def get_incident(incident_id: int, repository: Repository = Depends(get_repository)) -> IncidentDetailResponse:
    incident = repository.get_incident_by_id(incident_id)
    if incident is None:
        raise incident_not_found(incident_id)

    application = repository.get_application_by_id(incident.scope_id)
    evidence_count = len(repository.list_evidence_for_incident(incident_id))
    explanation_count = len(repository.list_explanations_for_incident(incident_id))

    return IncidentDetailResponse.from_domain(
        incident,
        application_key=application.key if application is not None else "?",
        application_name=application.name if application is not None else "?",
        evidence_count=evidence_count,
        explanation_count=explanation_count,
    )
