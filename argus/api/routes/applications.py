"""GET /api/v1/applications, /{application}, /{application}/history --
all three built from the exact same `argus.cli.queries` functions
`argus apps`/`argus inspect`/`argus history` already call. Lookup is by
name or key, case-insensitive, matching CLI semantics exactly (see
`queries.find_application_key`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query

from argus.api.dependencies import get_now, get_repository
from argus.api.errors import application_not_found, invalid_query_parameter
from argus.api.models import ApplicationDetailResponse, ApplicationHistoryResponse, ApplicationSummaryResponse, TransitionResponse
from argus.cli import queries
from argus.cli.durations import InvalidDurationError, parse_duration
from argus.cli.formatting import iso
from argus.domain.models import HealthStatus
from argus.store.repository import Repository

router = APIRouter()

_DEFAULT_HISTORY_SINCE = "24h"


@router.get("", response_model=list[ApplicationSummaryResponse], summary="List all known applications")
def list_applications(
    status: Optional[str] = Query(None, description="Filter to one HealthStatus value, e.g. UNHEALTHY"),
    repository: Repository = Depends(get_repository),
    now: datetime = Depends(get_now),
) -> list[ApplicationSummaryResponse]:
    summaries = queries.list_application_summaries(repository, now=now)

    if status is not None:
        try:
            wanted = HealthStatus(status.strip().upper())
        except ValueError:
            valid = ", ".join(s.value for s in HealthStatus)
            raise invalid_query_parameter(f"status {status!r} is not a valid HealthStatus (one of: {valid})")
        summaries = [summary for summary in summaries if summary.status is wanted]

    return [ApplicationSummaryResponse.from_domain(summary) for summary in summaries]


@router.get(
    "/{application}", response_model=ApplicationDetailResponse, summary="Detailed current view of one application"
)
def get_application(
    application: str,
    repository: Repository = Depends(get_repository),
    now: datetime = Depends(get_now),
) -> ApplicationDetailResponse:
    detail = queries.get_application_detail(repository, now=now, name_or_key=application)
    if detail is None:
        suggestion = queries.suggest_application_name(repository, application)
        raise application_not_found(application, suggestion=suggestion)
    return ApplicationDetailResponse.from_domain(detail)


@router.get(
    "/{application}/history",
    response_model=ApplicationHistoryResponse,
    summary="Chronological health transitions for one application",
)
def get_application_history(
    application: str,
    since: str = Query(_DEFAULT_HISTORY_SINCE, description="How far back to look, e.g. 30m/6h/24h/7d"),
    repository: Repository = Depends(get_repository),
    now: datetime = Depends(get_now),
) -> ApplicationHistoryResponse:
    try:
        delta = parse_duration(since)
    except InvalidDurationError as exc:
        raise invalid_query_parameter(str(exc))

    window_start = now - delta
    entries = queries.list_history(repository, name_or_key=application, since=window_start)
    if entries is None:
        suggestion = queries.suggest_application_name(repository, application)
        raise application_not_found(application, suggestion=suggestion)

    return ApplicationHistoryResponse(
        application=application,
        since=iso(window_start),
        transitions=[TransitionResponse.from_domain(entry) for entry in entries],
    )
