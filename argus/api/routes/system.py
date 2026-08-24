"""GET /api/v1/system/status -- the same core information `argus status
--json` already prints, built from the identical `argus.cli.queries`
read model. No live Docker access here -- see `argus.api.routes.doctor`
for the one route in this API that has any.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends

from argus.api.dependencies import get_now, get_repository
from argus.api.models import ApplicationSummaryResponse, CollectorStatusResponse, SystemStatusResponse
from argus.cli import queries
from argus.store.repository import Repository

router = APIRouter()


@router.get(
    "/status",
    response_model=SystemStatusResponse,
    summary="Collector liveness and every application's current status",
)
def get_status(
    repository: Repository = Depends(get_repository), now: datetime = Depends(get_now)
) -> SystemStatusResponse:
    collector_status = queries.get_collector_status(repository, now=now)
    applications = queries.list_application_summaries(repository, now=now)
    open_incidents = len(queries.list_incidents(repository, open_only=True))

    return SystemStatusResponse(
        collector=CollectorStatusResponse.from_domain(collector_status),
        applications=[ApplicationSummaryResponse.from_domain(app) for app in applications],
        open_incidents=open_incidents,
    )
