"""GET /api/v1/incidents/{incident_id}/evidence -- structured evidence
linked to one incident, via `argus.cli.queries.list_evidence_for_incident`
(the same read model `argus evidence --incident` already uses). Every
`sample` returned here is already-redacted, already-persisted text
(see `argus.evidence.redaction`) -- nothing here ever reads a raw log
line, and there is no code path back to Docker in this module.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from argus.api.dependencies import get_repository
from argus.api.errors import incident_not_found
from argus.api.models import EvidenceItemResponse, EvidenceResponse
from argus.cli import queries
from argus.store.repository import Repository

router = APIRouter()


@router.get(
    "/{incident_id}/evidence", response_model=EvidenceResponse, summary="Redacted evidence linked to one incident"
)
def get_incident_evidence(
    incident_id: int,
    limit: Optional[int] = Query(None, ge=1, description="Return only the most recent N evidence items"),
    repository: Repository = Depends(get_repository),
) -> EvidenceResponse:
    views = queries.list_evidence_for_incident(repository, incident_id=incident_id)
    if views is None:
        raise incident_not_found(incident_id)

    if limit is not None:
        views = views[-limit:]

    return EvidenceResponse(incident_id=incident_id, evidence=[EvidenceItemResponse.from_domain(v) for v in views])
