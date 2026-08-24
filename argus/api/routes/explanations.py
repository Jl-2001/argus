"""GET /api/v1/incidents/{incident_id}/explanations,
/{incident_id}/explanations/latest -- persisted, already-validated AI
explanations only, straight from `Repository.list_explanations_for_incident`.

This module imports nothing from `argus.ai` at all: `ExplanationRecord`
(from `argus.store.repository`) already carries every field the
response needs, including `response_json` -- the exact
`IncidentExplanation.to_dict()` JSON Argus itself validated and
persisted when the explanation was originally generated (see
`argus.ai.explain._dump_response_json`). `argus.api.models.ExplanationResponse.from_record`
parses that with plain `json.loads`, never reconstructs an
`IncidentExplanation` object, and never touches an AI provider. There
is no code path from this module to `anthropic` or `google.genai` --
see `tests/unit/test_api_incident_subresources.py`'s
"no provider calls" tests and `tests/unit/test_api_architecture_guard.py`.

"latest" convention: `404` means *the incident itself* doesn't exist;
an incident that exists but has no explanation yet returns `200` with
a `null` body -- these are different facts and must not share a status
code.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends

from argus.api.dependencies import get_repository
from argus.api.errors import incident_not_found
from argus.api.models import ExplanationResponse, ExplanationsListResponse
from argus.store.repository import Repository

router = APIRouter()


@router.get(
    "/{incident_id}/explanations",
    response_model=ExplanationsListResponse,
    summary="Every persisted explanation ever generated for one incident",
)
def list_incident_explanations(
    incident_id: int, repository: Repository = Depends(get_repository)
) -> ExplanationsListResponse:
    if repository.get_incident_by_id(incident_id) is None:
        raise incident_not_found(incident_id)

    records = repository.list_explanations_for_incident(incident_id)
    return ExplanationsListResponse(
        incident_id=incident_id, explanations=[ExplanationResponse.from_record(r) for r in records]
    )


@router.get(
    "/{incident_id}/explanations/latest",
    response_model=Optional[ExplanationResponse],
    summary="The most recently generated persisted explanation, or null if none exists yet",
)
def get_latest_incident_explanation(
    incident_id: int, repository: Repository = Depends(get_repository)
) -> Optional[ExplanationResponse]:
    if repository.get_incident_by_id(incident_id) is None:
        raise incident_not_found(incident_id)

    records = repository.list_explanations_for_incident(incident_id)  # oldest first
    if not records:
        return None
    return ExplanationResponse.from_record(records[-1])
