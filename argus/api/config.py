"""The API's own small config surface: CORS origins and the bind
address. Deliberately not a competing config system -- the database
path itself is resolved by reusing
``argus.store.database.default_database_path`` directly (see
``create_app`` in ``argus.api.app``), not reimplemented here.
"""

from __future__ import annotations

import os

__all__ = [
    "DEFAULT_CORS_ORIGINS",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "resolve_cors_origins",
    "resolve_host",
    "resolve_port",
]

#: The future React dev server's own default origin, in both the
#: hostname and loopback-IP forms browsers can send as `Origin`. Never
#: `["*"]` -- see `argus.api.app.create_app`'s own docstring for why.
DEFAULT_CORS_ORIGINS: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")

#: Loopback-only by default -- this API is not exposed publicly unless
#: a caller explicitly overrides host/port (see `argus.api.app.run`).
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8088

_CORS_ENV_VAR = "ARGUS_API_CORS_ORIGINS"
_HOST_ENV_VAR = "ARGUS_API_HOST"
_PORT_ENV_VAR = "ARGUS_API_PORT"


def resolve_cors_origins(explicit: "tuple[str, ...] | None" = None) -> tuple[str, ...]:
    """Precedence: ``explicit`` (e.g. a `create_app` kwarg) >
    ``ARGUS_API_CORS_ORIGINS`` (a comma-separated list) >
    `DEFAULT_CORS_ORIGINS`. Mirrors `default_database_path`'s own
    explicit-arg-over-env-var-over-default shape, so there is exactly
    one pattern for "how does this API resolve a configurable value",
    not a new one invented per setting.
    """

    if explicit is not None:
        return tuple(explicit)
    raw = os.environ.get(_CORS_ENV_VAR)
    if raw:
        origins = tuple(origin.strip() for origin in raw.split(",") if origin.strip())
        if origins:
            return origins
    return DEFAULT_CORS_ORIGINS


def resolve_host(explicit: "str | None" = None) -> str:
    if explicit is not None:
        return explicit
    return os.environ.get(_HOST_ENV_VAR, DEFAULT_HOST)


def resolve_port(explicit: "int | None" = None) -> int:
    if explicit is not None:
        return explicit
    raw = os.environ.get(_PORT_ENV_VAR)
    return int(raw) if raw else DEFAULT_PORT
