"""GET /api/v1/hosts, /{host_key} -- read-only, user-facing views of
every registered monitored host (Milestone 16). Built from the exact
same `argus.cli.queries.list_host_views`/`get_host_detail` functions a
future `argus hosts` CLI command would use -- same "one read model, two
transports" discipline every other route in this package already
follows (see `argus.api.models`'s own docstring).

Deliberately separate from `argus.api.routes.agents` (machine
ingestion, POST-only) -- see that module's own docstring on why the
two are two different route modules under two different URL prefixes.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends

from argus.api.dependencies import get_now, get_repository
from argus.api.errors import host_not_found
from argus.api.models import HostDetailResponse, HostSummaryResponse
from argus.cli import queries
from argus.store.repository import Repository

router = APIRouter()


@router.get("", response_model=list[HostSummaryResponse], summary="List all registered hosts")
def list_hosts(
    repository: Repository = Depends(get_repository), now: datetime = Depends(get_now)
) -> list[HostSummaryResponse]:
    return [HostSummaryResponse.from_domain(view) for view in queries.list_host_views(repository, now=now)]


@router.get("/{host_key}", response_model=HostDetailResponse, summary="Detail for one host")
def get_host(
    host_key: str, repository: Repository = Depends(get_repository), now: datetime = Depends(get_now)
) -> HostDetailResponse:
    detail = queries.get_host_detail(repository, now=now, host_key=host_key)
    if detail is None:
        raise host_not_found(host_key)
    return HostDetailResponse.from_domain(detail)
