"""Milestone 13 -- `create_app()` itself: construction against a temp
DB, the empty-database baseline, database-unavailable error handling,
OpenAPI generation, and the CORS policy. Endpoint-specific behavior
lives in the other `test_api_*.py` files.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from argus.api.app import create_app
from argus.api.config import DEFAULT_CORS_ORIGINS


def make_client(db_path: Path) -> TestClient:
    return TestClient(create_app(database_path=db_path))


class TestAppCreation:
    def test_create_app_works_against_a_temp_db(self, tmp_path):
        app = create_app(database_path=tmp_path / "argus.db")
        assert app.title == "Argus API"

    def test_create_app_defaults_database_path_the_same_way_the_cli_does(self, monkeypatch, tmp_path):
        # No explicit database_path, no ARGUS_DB_PATH -- falls back to
        # argus.store.database.DEFAULT_DB_PATH, exactly like the CLI.
        monkeypatch.delenv("ARGUS_DB_PATH", raising=False)
        monkeypatch.chdir(tmp_path)
        app = create_app()
        assert str(app.state.database_path) == "data/argus.db" or str(app.state.database_path).endswith(
            "data/argus.db"
        )

    def test_create_app_honors_argus_db_path_env_var(self, monkeypatch, tmp_path):
        target = tmp_path / "elsewhere.db"
        monkeypatch.setenv("ARGUS_DB_PATH", str(target))
        app = create_app()
        assert Path(app.state.database_path) == target

    def test_explicit_database_path_overrides_env_var(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ARGUS_DB_PATH", str(tmp_path / "ignored.db"))
        explicit = tmp_path / "explicit.db"
        app = create_app(database_path=explicit)
        assert Path(app.state.database_path) == explicit


class TestEmptyDatabase:
    def test_status_on_a_never_run_empty_database(self, tmp_path):
        client = make_client(tmp_path / "argus.db")
        response = client.get("/api/v1/system/status")
        assert response.status_code == 200
        body = response.json()
        assert body["collector"]["status"] == "NEVER_RUN"
        assert body["applications"] == []
        assert body["open_incidents"] == 0

    def test_applications_list_is_empty(self, tmp_path):
        client = make_client(tmp_path / "argus.db")
        response = client.get("/api/v1/applications")
        assert response.status_code == 200
        assert response.json() == []

    def test_incidents_list_is_empty(self, tmp_path):
        client = make_client(tmp_path / "argus.db")
        response = client.get("/api/v1/incidents")
        assert response.status_code == 200
        assert response.json() == {"incidents": []}


class TestDatabaseUnavailable:
    def test_malformed_database_file_returns_a_clean_503_not_a_traceback(self, tmp_path):
        bad_db = tmp_path / "not-a-database.db"
        bad_db.write_bytes(b"this is not a sqlite file at all, just garbage bytes")

        client = make_client(bad_db)
        response = client.get("/api/v1/system/status")

        assert response.status_code == 503
        body = response.json()
        assert body["error"]["code"] == "database_unavailable"
        # no traceback, no raw exception repr/class name leaked
        assert "Traceback" not in body["error"]["message"]
        assert "sqlite3." not in body["error"]["message"]

    def test_malformed_database_does_not_prevent_a_later_request_against_a_good_db(self, tmp_path):
        # Each request opens its own connection (argus.api.dependencies.get_repository)
        # -- one failed request must never poison later ones.
        bad_db = tmp_path / "not-a-database.db"
        bad_db.write_bytes(b"garbage")
        good_db = tmp_path / "good.db"

        bad_client = make_client(bad_db)
        assert bad_client.get("/api/v1/system/status").status_code == 503

        good_client = make_client(good_db)
        assert good_client.get("/api/v1/system/status").status_code == 200


class TestOpenAPI:
    def test_openapi_json_generates_successfully(self, tmp_path):
        client = make_client(tmp_path / "argus.db")
        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert schema["info"]["title"] == "Argus API"
        assert "/api/v1/system/status" in schema["paths"]

    def test_docs_page_is_served(self, tmp_path):
        client = make_client(tmp_path / "argus.db")
        response = client.get("/docs")
        assert response.status_code == 200


class TestCORS:
    def test_default_cors_origins_are_localhost_only(self):
        assert DEFAULT_CORS_ORIGINS == ("http://localhost:5173", "http://127.0.0.1:5173")
        assert "*" not in DEFAULT_CORS_ORIGINS

    def test_localhost_5173_origin_is_allowed(self, tmp_path):
        client = make_client(tmp_path / "argus.db")
        response = client.get(
            "/api/v1/system/status", headers={"Origin": "http://localhost:5173"}
        )
        assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"

    def test_unknown_origin_is_not_broadly_allowed(self, tmp_path):
        client = make_client(tmp_path / "argus.db")
        response = client.get(
            "/api/v1/system/status", headers={"Origin": "https://evil.example.com"}
        )
        # The request itself still succeeds (GET requests aren't blocked
        # by the browser's CORS check pre-flight for a simple GET), but
        # the CORS middleware must not echo back / widen access to an
        # unrecognized origin.
        assert response.headers.get("access-control-allow-origin") != "https://evil.example.com"
        assert response.headers.get("access-control-allow-origin") != "*"

    def test_credentials_are_never_allowed(self, tmp_path):
        client = make_client(tmp_path / "argus.db")
        response = client.get(
            "/api/v1/system/status", headers={"Origin": "http://localhost:5173"}
        )
        assert response.headers.get("access-control-allow-credentials") != "true"

    def test_custom_cors_origins_override_the_default(self, tmp_path):
        app = create_app(database_path=tmp_path / "argus.db", cors_origins=["https://dashboard.example.com"])
        client = TestClient(app)
        response = client.get(
            "/api/v1/system/status", headers={"Origin": "https://dashboard.example.com"}
        )
        assert response.headers.get("access-control-allow-origin") == "https://dashboard.example.com"
        # and the default is no longer allowed once overridden
        response2 = client.get("/api/v1/system/status", headers={"Origin": "http://localhost:5173"})
        assert response2.headers.get("access-control-allow-origin") != "http://localhost:5173"
