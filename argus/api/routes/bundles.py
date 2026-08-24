"""GET /api/v1/incidents/{incident_id}/bundle -- the exact deterministic,
bounded evidence bundle `argus bundle --json` already prints, via the
same `argus.evidence.assembler.assemble_evidence_bundle`. Assembling a
bundle at request time is safe here because it is read-only and
deterministic given already-persisted state -- this route never calls
an AI provider and never writes anything. One `now` (see
`argus.api.dependencies.get_now`) is threaded through as the bundle's
single `generated_at`.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends

from argus.api.dependencies import get_now, get_repository
from argus.api.errors import incident_not_found
from argus.api.models import EvidenceBundleResponse
from argus.evidence.assembler import DEFAULT_ASSEMBLER_CONFIG, IncidentNotFoundError, assemble_evidence_bundle
from argus.store.repository import Repository

router = APIRouter()


@router.get(
    "/{incident_id}/bundle",
    response_model=EvidenceBundleResponse,
    summary="The deterministic, bounded evidence bundle for one incident",
)
def get_incident_bundle(
    incident_id: int,
    repository: Repository = Depends(get_repository),
    now: datetime = Depends(get_now),
) -> EvidenceBundleResponse:
    try:
        bundle = assemble_evidence_bundle(repository, incident_id, now=now, config=DEFAULT_ASSEMBLER_CONFIG)
    except IncidentNotFoundError:
        raise incident_not_found(incident_id)

    return EvidenceBundleResponse.model_validate(bundle.to_dict())
