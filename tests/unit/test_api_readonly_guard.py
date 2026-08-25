"""Milestone 13 -- proves the entire `/api/v1` surface is provably
read-only: every route responds to GET only (HEAD/OPTIONS, which
Starlette derives automatically, are fine); no route anywhere in the
app exposes POST/PUT/PATCH/DELETE.

Milestone 16 adds the one deliberate, named exception to that:
`POST /api/v1/agents/ingest` -- machine-to-machine snapshot ingestion,
never a dashboard/user action (see `argus.api.routes.agents`'s own
docstring). Every assertion below still holds for every *other* route;
the exception is checked explicitly by name, not silently excluded, so
a second mutating route added anywhere else in this app is still
caught.

Route/method inspection goes through `app.openapi()["paths"]` rather
than walking `app.routes` directly -- the shape of `app.routes` itself
(whether a mounted sub-router flattens eagerly or lazily) is a FastAPI
implementation detail that has changed across versions; the OpenAPI
schema is the one stable, version-independent statement of "what HTTP
methods does this app actually serve at this path", and it's exactly
what a real client (or the future React dashboard) would see too.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from argus.api.app import create_app

_MUTATING_METHODS = {"post", "put", "patch", "delete"}

#: The one path/method allowed to be mutating -- see this file's own
#: docstring. Named explicitly so every assertion below can hold
#: everywhere else without exception.
_AGENT_INGEST_PATH = "/api/v1/agents/ingest"


class TestNoMutatingRoutesAreRegistered:
    def test_no_api_route_exposes_a_mutating_http_method(self, tmp_path):
        app = create_app(database_path=tmp_path / "a.db")
        schema = app.openapi()

        offenders = [
            (path, _MUTATING_METHODS & set(methods.keys()))
            for path, methods in schema["paths"].items()
            if path != _AGENT_INGEST_PATH and (_MUTATING_METHODS & set(methods.keys()))
        ]
        assert not offenders, f"mutating HTTP method(s) exposed: {offenders}"

    def test_every_api_v1_route_is_get_only_except_the_one_named_agent_ingest_exception(self, tmp_path):
        app = create_app(database_path=tmp_path / "a.db")
        schema = app.openapi()

        v1_paths = {path: methods for path, methods in schema["paths"].items() if path.startswith("/api/v1")}
        assert v1_paths, "expected at least one /api/v1 route to exist"
        for path, methods in v1_paths.items():
            if path == _AGENT_INGEST_PATH:
                continue
            assert set(methods.keys()) == {"get"}, f"{path} exposes non-GET method(s): {set(methods.keys())}"

    def test_the_one_agent_ingest_exception_is_exactly_post_and_nothing_else(self, tmp_path):
        # Guards the exception itself from silently widening -- it must
        # be POST-only, never also GET/PUT/PATCH/DELETE.
        app = create_app(database_path=tmp_path / "a.db")
        schema = app.openapi()
        assert schema["paths"][_AGENT_INGEST_PATH].keys() == {"post"}


class TestMutatingRequestsAreRejected:
    def test_post_put_patch_delete_all_return_405(self, tmp_path):
        db_path = tmp_path / "a.db"
        client = TestClient(create_app(database_path=db_path))

        for path in ("/api/v1/system/status", "/api/v1/applications", "/api/v1/incidents", "/api/v1/hosts"):
            for method in ("post", "put", "patch", "delete"):
                response = getattr(client, method)(path)
                assert response.status_code == 405, f"{method.upper()} {path} was not rejected"

    def test_agent_ingest_rejects_every_method_except_post(self, tmp_path):
        client = TestClient(create_app(database_path=tmp_path / "a.db"))
        for method in ("get", "put", "patch", "delete"):
            response = getattr(client, method)(_AGENT_INGEST_PATH)
            assert response.status_code == 405, f"{method.upper()} {_AGENT_INGEST_PATH} was not rejected"

    def test_405_response_uses_the_standard_error_envelope(self, tmp_path):
        client = TestClient(create_app(database_path=tmp_path / "a.db"))
        response = client.post("/api/v1/applications")
        assert response.status_code == 405
        body = response.json()
        assert "error" in body
        assert "code" in body["error"]
        assert "message" in body["error"]


class TestReadOnlyGuardCannotBeSilentlyBypassed:
    def test_no_route_module_defines_a_mutating_decorator_except_the_one_named_agents_exception(self):
        # A second, independent check at the source level: no
        # argus.api.routes.* module contains a "@router.post/put/patch/
        # delete(" decorator anywhere in its text -- catches a mutating
        # route added but never actually exercised by a test above.
        # `agents.py` is the one named, deliberate exception (see this
        # file's own docstring) -- checked by name, not silently
        # excluded, and only for `post`.
        routes_dir = Path(__file__).resolve().parents[2] / "argus" / "api" / "routes"
        mutating_decorator = re.compile(r"@\w+\.(post|put|patch|delete)\s*\(")
        offenders = []
        for path in sorted(routes_dir.glob("*.py")):
            if path.name == "agents.py":
                continue
            match = mutating_decorator.search(path.read_text())
            if match:
                offenders.append((path.name, match.group(1)))
        assert not offenders, f"a route module defines a mutating decorator: {offenders}"

    def test_agents_module_defines_exactly_one_post_decorator_and_nothing_else_mutating(self):
        routes_dir = Path(__file__).resolve().parents[2] / "argus" / "api" / "routes"
        source = (routes_dir / "agents.py").read_text()
        assert len(re.findall(r"@\w+\.post\s*\(", source)) == 1
        assert not re.search(r"@\w+\.(put|patch|delete)\s*\(", source)
