"""GET /api/v1/system/doctor -- the one deliberate exception to this
API's "normal routes never touch Docker/argus.collectors" rule.

`argus doctor` performs live, read-only Docker diagnostics
(`argus.doctor.checks.run_checks`): this route calls that exact same,
already-existing subsystem, unchanged -- no repairs, no migrations, no
Docker mutation, GET only, same as every other route in this API. It
is kept in its own module, separate from `system.py`'s `/status`,
specifically so `tests/unit/test_api_architecture_guard.py` can assert
that *every other* `argus.api.routes` module stays free of
`docker`/`argus.collectors`/`argus.doctor`, while this one file is the
sole, named, intentional exception -- not something a future route
could quietly start doing by accident.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request

from argus.api.dependencies import get_now
from argus.api.models import DoctorResponse
from argus.doctor.checks import run_checks

router = APIRouter()


@router.get(
    "/doctor",
    response_model=DoctorResponse,
    summary="Live, read-only prerequisite diagnostics (the one route that touches Docker)",
)
def get_doctor(request: Request, now: datetime = Depends(get_now)) -> DoctorResponse:
    result = run_checks(db_path=request.app.state.database_path, now=now)
    return DoctorResponse.from_domain(result)
