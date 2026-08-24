"""The one error shape every `/api/v1` response uses:

    {"error": {"code": "...", "message": "..."}}

`APIError` is the single exception type route/dependency code raises
to produce it; `argus.api.app.create_app` registers the handlers below
that turn it (and a couple of well-known lower-layer exceptions) into
that JSON shape at the right status code. Nothing here ever leaks a
raw stack trace or exception repr to the client -- see
`_handle_unexpected_error`.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from argus.store.database import DatabaseOpenError, SchemaError

__all__ = [
    "APIError",
    "application_not_found",
    "incident_not_found",
    "database_unavailable",
    "invalid_query_parameter",
    "register_exception_handlers",
]


class APIError(Exception):
    """One typed error, carrying exactly what the ``{"error": {...}}``
    envelope needs. Raised directly from route/dependency code; never
    constructed from an arbitrary caught exception's own message (that
    would risk leaking internals) -- see the small factory functions
    below for the handful of error conditions this API actually
    produces on purpose.
    """

    def __init__(self, *, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def application_not_found(name_or_key: str, *, suggestion: "str | None" = None) -> APIError:
    message = f"Application {name_or_key!r} was not found."
    if suggestion is not None:
        message += f" Did you mean {suggestion!r}?"
    return APIError(code="application_not_found", message=message, status_code=404)


def incident_not_found(incident_id: int) -> APIError:
    return APIError(
        code="incident_not_found", message=f"Incident #{incident_id} was not found.", status_code=404
    )


def database_unavailable(detail: str) -> APIError:
    # `detail` is always one of our own `DatabaseOpenError`/`SchemaError`
    # messages -- already-sanitized, never a raw traceback -- so it's
    # safe to surface directly, same as `argus.cli.main` printing it to
    # stderr today.
    return APIError(
        code="database_unavailable",
        message=f"The Argus database is currently unavailable: {detail}",
        status_code=503,
    )


def invalid_query_parameter(message: str) -> APIError:
    return APIError(code="invalid_query_parameter", message=message, status_code=422)


def _envelope(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def register_exception_handlers(app: FastAPI) -> None:
    """Wires every handler this API needs onto `app`. Called once, from
    `create_app` -- kept out of `create_app` itself purely so that
    module stays about composition, not error-shape detail."""

    @app.exception_handler(APIError)
    async def _handle_api_error(request: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=_envelope(exc.code, exc.message))

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # FastAPI's own 404 (unknown route) / 405 (wrong method) etc.
        # still get the same envelope shape as our own errors, rather
        # than Starlette's default `{"detail": "..."}`.
        code = "not_found" if exc.status_code == 404 else "http_error"
        message = str(exc.detail) if exc.detail else "Request failed."
        return JSONResponse(status_code=exc.status_code, content=_envelope(code, message))

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422, content=_envelope("invalid_request", "The request was invalid: " + str(exc))
        )

    @app.exception_handler(DatabaseOpenError)
    async def _handle_db_open_error(request: Request, exc: DatabaseOpenError) -> JSONResponse:
        return JSONResponse(status_code=503, content=_envelope("database_unavailable", str(exc)))

    @app.exception_handler(SchemaError)
    async def _handle_schema_error(request: Request, exc: SchemaError) -> JSONResponse:
        return JSONResponse(status_code=503, content=_envelope("database_unavailable", str(exc)))

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # Deliberately generic: no exception message, no repr, no
        # traceback -- an unanticipated failure must never leak
        # internals to the client. Real detail belongs in server logs,
        # not the response body.
        return JSONResponse(
            status_code=500, content=_envelope("internal_error", "An internal error occurred.")
        )
