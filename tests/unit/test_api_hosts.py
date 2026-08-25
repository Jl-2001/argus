"""Milestone 16 -- GET /api/v1/hosts, /{host_key}: read-only, never
exposes token hash or any authentication metadata."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from argus.api.app import create_app
from argus.security import generate_token, hash_token
from argus.store.database import open_database
from argus.store.repository import Repository

UTC = timezone.utc


def real_now() -> datetime:
    return datetime.now(UTC)


def register_host(db_path, *, host_key="dell", agent_id="agent-1", display_name="Dell") -> str:
    token = generate_token()
    conn = open_database(db_path)
    try:
        Repository(conn).create_agent_host(
            host_key=host_key, agent_id=agent_id, display_name=display_name,
            token_hash=hash_token(token), now=real_now(),
        )
    finally:
        conn.close()
    return token


class TestListHosts:
    def test_local_host_always_present(self, tmp_path):
        db_path = tmp_path / "a.db"
        # Bootstrapping the database (any open_database call) creates
        # the local host row -- no agent needs to be registered first.
        open_database(db_path).close()
        client = TestClient(create_app(database_path=db_path))
        response = client.get("/api/v1/hosts")
        assert response.status_code == 200
        keys = {h["host_key"] for h in response.json()}
        assert "local" in keys

    def test_registered_agent_host_appears(self, tmp_path):
        db_path = tmp_path / "a.db"
        register_host(db_path)
        client = TestClient(create_app(database_path=db_path))
        response = client.get("/api/v1/hosts")
        keys = {h["host_key"] for h in response.json()}
        assert "dell" in keys

    def test_response_never_includes_token_hash_or_agent_id(self, tmp_path):
        db_path = tmp_path / "a.db"
        register_host(db_path)
        client = TestClient(create_app(database_path=db_path))
        response = client.get("/api/v1/hosts")
        text = response.text
        assert "agent_token_hash" not in text
        assert "token_hash" not in text
        assert "agent_id" not in text


class TestGetHost:
    def test_unknown_host_key_is_404(self, tmp_path):
        db_path = tmp_path / "a.db"
        open_database(db_path).close()
        client = TestClient(create_app(database_path=db_path))
        response = client.get("/api/v1/hosts/nonexistent")
        assert response.status_code == 404

    def test_known_host_returns_detail(self, tmp_path):
        db_path = tmp_path / "a.db"
        register_host(db_path, host_key="dell", display_name="Ubuntu Dell")
        client = TestClient(create_app(database_path=db_path))
        response = client.get("/api/v1/hosts/dell")
        assert response.status_code == 200
        body = response.json()
        assert body["display_name"] == "Ubuntu Dell"
        assert body["kind"] == "agent"
        assert "applications" in body

    def test_detail_never_includes_token_hash(self, tmp_path):
        db_path = tmp_path / "a.db"
        register_host(db_path, host_key="dell")
        client = TestClient(create_app(database_path=db_path))
        response = client.get("/api/v1/hosts/dell")
        assert "token_hash" not in response.text
