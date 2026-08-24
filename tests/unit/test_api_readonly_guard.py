"""Milestone 13 -- proves the entire `/api/v1` surface is provably
read-only: every route responds to GET only (HEAD/OPTIONS, which
Starlette derives automatically, are fine); no route anywhere in the
app exposes POST/PUT/PATCH/DELETE.

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


class TestNoMutatingRoutesAreRegistered:
    def test_no_api_route_exposes_a_mutating_http_method(self, tmp_path):
        app = create_app(database_path=tmp_path / "a.db")
        schema = app.openapi()

        offenders = [
            (path, _MUTATING_METHODS & set(methods.keys()))
            for path, methods in schema["paths"].items()
            if _MUTATING_METHODS & set(methods.keys())
        ]
        assert not offenders, f"mutating HTTP method(s) exposed: {offenders}"

    def test_every_api_v1_route_is_get_only(self, tmp_path):
        app = create_app(database_path=tmp_path / "a.db")
        schema = app.openapi()

        v1_paths = {path: methods for path, methods in schema["paths"].items() if path.startswith("/api/v1")}
        assert v1_paths, "expected at least one /api/v1 route to exist"
        for path, methods in v1_paths.items():
            assert set(methods.keys()) == {"get"}, f"{path} exposes non-GET method(s): {set(methods.keys())}"


class TestMutatingRequestsAreRejected:
    def test_post_put_patch_delete_all_return_405(self, tmp_path):
        db_path = tmp_path / "a.db"
        client = TestClient(create_app(database_path=db_path))

        for path in ("/api/v1/system/status", "/api/v1/applications", "/api/v1/incidents"):
            for method in ("post", "put", "patch", "delete"):
                response = getattr(client, method)(path)
                assert response.status_code == 405, f"{method.upper()} {path} was not rejected"

    def test_405_response_uses_the_standard_error_envelope(self, tmp_path):
        client = TestClient(create_app(database_path=tmp_path / "a.db"))
        response = client.post("/api/v1/applications")
        assert response.status_code == 405
        body = response.json()
        assert "error" in body
        assert "code" in body["error"]
        assert "message" in body["error"]


class TestReadOnlyGuardCannotBeSilentlyBypassed:
    def test_no_route_module_defines_a_mutating_decorator(self):
        # A second, independent check at the source level: no
        # argus.api.routes.* module contains a "@router.post/put/patch/
        # delete(" decorator anywhere in its text -- catches a mutating
        # route added but never actually exercised by a test above.
        routes_dir = Path(__file__).resolve().parents[2] / "argus" / "api" / "routes"
        mutating_decorator = re.compile(r"@\w+\.(post|put|patch|delete)\s*\(")
        offenders = []
        for path in sorted(routes_dir.glob("*.py")):
            match = mutating_decorator.search(path.read_text())
            if match:
                offenders.append((path.name, match.group(1)))
        assert not offenders, f"a route module defines a mutating decorator: {offenders}"
