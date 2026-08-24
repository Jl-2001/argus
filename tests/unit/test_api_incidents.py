"""Milestone 13 -- GET /api/v1/incidents, /{incident_id}."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from api_fixtures import seed_incident_stack
from argus.api.app import create_app
from argus.domain.models import HealthStatus
from argus.store.database import open_database
from argus.store.repository import Repository


class TestIncidentsList:
    def test_all_vs_open_filtering(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed = seed_incident_stack(db_path)

        # Resolve the incident so "all" and "open" genuinely differ.
        conn = open_database(db_path)
        repo = Repository(conn)
        transition_id = repo.insert_transition(
            scope="application", scope_id=seed["application_id"], from_status=HealthStatus.UNHEALTHY,
            to_status=HealthStatus.HEALTHY, occurred_at=seed["now"] + timedelta(minutes=1),
        )
        repo.resolve_incident(
            incident_id=seed["incident_id"], closed_at=seed["now"] + timedelta(minutes=1),
            resolving_transition_id=transition_id,
        )
        conn.close()

        client = TestClient(create_app(database_path=db_path))

        all_incidents = client.get("/api/v1/incidents").json()["incidents"]
        assert len(all_incidents) == 1
        assert all_incidents[0]["status"] == "resolved"

        open_incidents = client.get("/api/v1/incidents?status=open").json()["incidents"]
        assert open_incidents == []

    def test_newest_first(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed_incident_stack(db_path, key="alpha", name="Alpha")
        seed_incident_stack(db_path, key="beta", name="Beta", with_explanation=False)

        client = TestClient(create_app(database_path=db_path))
        body = client.get("/api/v1/incidents").json()["incidents"]
        assert [i["opened_at"] for i in body] == sorted((i["opened_at"] for i in body), reverse=True)

    def test_incident_fields(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed_incident_stack(db_path)
        client = TestClient(create_app(database_path=db_path))
        incident = client.get("/api/v1/incidents").json()["incidents"][0]
        assert set(incident.keys()) == {
            "id", "application", "application_key", "status", "opened_at", "closed_at",
            "opening_status", "worst_status", "failure_signature",
        }
        assert incident["failure_signature"] == "application:cnstrct"

    def test_invalid_status_filter_is_a_clean_422(self, tmp_path):
        client = TestClient(create_app(database_path=tmp_path / "a.db"))
        response = client.get("/api/v1/incidents?status=bogus")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_query_parameter"


class TestIncidentDetail:
    def test_correct_metadata(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed = seed_incident_stack(db_path)

        client = TestClient(create_app(database_path=db_path))
        body = client.get(f"/api/v1/incidents/{seed['incident_id']}").json()

        assert body["id"] == seed["incident_id"]
        assert body["application_key"] == "cnstrct"
        assert body["application_name"] == "CNSTRCT"
        assert body["status"] == "open"
        assert body["evidence_count"] == 1
        assert body["explanation_count"] == 1
        assert body["has_cached_explanation"] is True

    def test_incident_with_no_explanation_reports_that_honestly(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed = seed_incident_stack(db_path, with_explanation=False)
        client = TestClient(create_app(database_path=db_path))
        body = client.get(f"/api/v1/incidents/{seed['incident_id']}").json()
        assert body["explanation_count"] == 0
        assert body["has_cached_explanation"] is False

    def test_404_for_unknown_incident(self, tmp_path):
        client = TestClient(create_app(database_path=tmp_path / "a.db"))
        response = client.get("/api/v1/incidents/999999")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "incident_not_found"
