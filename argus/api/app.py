"""The FastAPI application factory.

    SQLite / existing services (argus.store, argus.cli.queries,
                                 argus.evidence.assembler)
        |
        v
    argus.api  (this package -- read-only, /api/v1, GET only)
        |
        v
    future React dashboard

`create_app` builds a fresh `FastAPI` instance against an explicit
database path -- there is no mutable global connection opened at
import time, so a test can build as many independent apps against as
many temporary databases as it wants. Local-only by default: see
`run` for the bind address, and `argus.api.config` for the CORS
policy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from argus.api.config import resolve_cors_origins, resolve_host, resolve_port
from argus.api.errors import register_exception_handlers
from argus.api.routes import applications, bundles, doctor, evidence, explanations, incidents, system
from argus.store.database import default_database_path

__all__ = ["create_app", "run"]

_API_V1_PREFIX = "/api/v1"


def create_app(
    *,
    database_path: "str | Path | None" = None,
    cors_origins: "Sequence[str] | None" = None,
) -> FastAPI:
    """Builds one Argus read API application.

    `database_path` follows exactly the same precedence
    `argus.store.database.default_database_path` already defines for
    the CLI (explicit path > `ARGUS_DB_PATH` > `./data/argus.db`) --
    this is not a second, competing config mechanism, just that same
    resolution reused. Pass a `tmp_path`-backed path in tests for a
    fully isolated database per test.

    `cors_origins` defaults to `argus.api.config.DEFAULT_CORS_ORIGINS`
    (the local Vite dev server only) via `ARGUS_API_CORS_ORIGINS` --
    never `["*"]`, since a wide-open origin policy would be unsafe the
    moment any endpoint here starts accepting credentials.
    """

    resolved_db_path = default_database_path(database_path)
    resolved_cors = resolve_cors_origins(tuple(cors_origins) if cors_origins is not None else None)

    app = FastAPI(
        title="Argus API",
        description=(
            "A read-only presentation layer over Argus's deterministic monitoring "
            "substrate and persisted AI explanations. Every /api/v1 route is GET only."
        ),
        version="1",
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.database_path = resolved_db_path

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved_cors),
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    app.include_router(system.router, prefix=f"{_API_V1_PREFIX}/system", tags=["System"])
    app.include_router(doctor.router, prefix=f"{_API_V1_PREFIX}/system", tags=["System"])
    app.include_router(applications.router, prefix=f"{_API_V1_PREFIX}/applications", tags=["Applications"])
    app.include_router(incidents.router, prefix=f"{_API_V1_PREFIX}/incidents", tags=["Incidents"])
    app.include_router(evidence.router, prefix=f"{_API_V1_PREFIX}/incidents", tags=["Evidence"])
    app.include_router(bundles.router, prefix=f"{_API_V1_PREFIX}/incidents", tags=["Evidence"])
    app.include_router(explanations.router, prefix=f"{_API_V1_PREFIX}/incidents", tags=["AI"])

    return app


def run() -> None:
    """Console entry point (`argus-api` -- see `[project.scripts]` in
    pyproject.toml). Thin on purpose: server config (host/port/CORS/db
    path) is resolved by `argus.api.config`/`create_app` themselves, not
    duplicated here.

    Binds to `127.0.0.1` by default, never `0.0.0.0` -- this API is not
    exposed publicly unless a caller explicitly overrides
    `ARGUS_API_HOST`. Equivalent to running:

        uvicorn argus.api.app:create_app --factory --host 127.0.0.1 --port 8088
    """

    import uvicorn

    uvicorn.run(create_app(), host=resolve_host(), port=resolve_port())
