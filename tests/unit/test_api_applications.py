"""Milestone 13 -- GET /api/v1/applications, /{application},
/{application}/history.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from api_fixtures import seed_incident_stack
from argus.api.app import create_app


class TestApplicationList:
    def test_ordering_and_status(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed_incident_stack(db_path, key="alpha", name="Alpha")
        seed_incident_stack(db_path, key="beta", name="Beta", with_explanation=False)

        client = TestClient(create_app(database_path=db_path))
        body = client.get("/api/v1/applications").json()

        assert [a["key"] for a in body] == ["alpha", "beta"]  # key ascending, deterministic
        assert all(a["status"] == "UNHEALTHY" for a in body)
        assert all(
            set(a.keys())
            == {"key", "name", "status", "services", "containers", "last_seen_at", "host_key", "host_name"}
            for a in body
        )
        # Milestone 16 -- every seeded application here predates hosts
        # entirely, so both are the local host's fixed labels.
        assert all(a["host_key"] == "local" for a in body)

    def test_status_query_filter(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed_incident_stack(db_path, key="alpha", name="Alpha")

        client = TestClient(create_app(database_path=db_path))
        matching = client.get("/api/v1/applications?status=UNHEALTHY")
        assert matching.status_code == 200
        assert len(matching.json()) == 1

        non_matching = client.get("/api/v1/applications?status=HEALTHY")
        assert non_matching.status_code == 200
        assert non_matching.json() == []

    def test_invalid_status_query_filter_is_a_clean_422(self, tmp_path):
        client = TestClient(create_app(database_path=tmp_path / "a.db"))
        response = client.get("/api/v1/applications?status=NOT_A_REAL_STATUS")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_query_parameter"


class TestApplicationDetail:
    def test_correct_nested_shape(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed = seed_incident_stack(db_path)

        client = TestClient(create_app(database_path=db_path))
        body = client.get("/api/v1/applications/cnstrct").json()

        assert body["key"] == "cnstrct"
        assert body["name"] == "CNSTRCT"
        assert len(body["services"]) == 1
        service = body["services"][0]
        assert service["compose_service"] == "web"
        # no container observation was ever inserted, so container detail is None
        assert service["container"] is None
        assert body["open_incident"]["id"] == seed["incident_id"]
        assert body["open_incident"]["opening_status"] == "UNHEALTHY"

    def test_lookup_is_case_insensitive_by_name_or_key(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed_incident_stack(db_path, key="cnstrct", name="CNSTRCT")

        client = TestClient(create_app(database_path=db_path))
        assert client.get("/api/v1/applications/CNSTRCT").status_code == 200
        assert client.get("/api/v1/applications/cnstrct").status_code == 200
        assert client.get("/api/v1/applications/Cnstrct").status_code == 200

    def test_secrets_never_appear_in_the_response(self, tmp_path):
        # No env vars, no raw labels, no host mount paths -- see the
        # milestone's own "Security" section.
        db_path = tmp_path / "a.db"
        seed_incident_stack(db_path)
        client = TestClient(create_app(database_path=db_path))
        body = client.get("/api/v1/applications/cnstrct")
        for forbidden_field in ("environment", "env", "labels", "mounts", "volumes"):
            assert forbidden_field not in body.json()


class TestApplicationNotFound:
    def test_404_with_stable_error_json(self, tmp_path):
        client = TestClient(create_app(database_path=tmp_path / "a.db"))
        response = client.get("/api/v1/applications/does-not-exist")
        assert response.status_code == 404
        body = response.json()
        assert body["error"]["code"] == "application_not_found"
        assert "does-not-exist" in body["error"]["message"]

    def test_404_includes_a_did_you_mean_suggestion_when_close(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed_incident_stack(db_path, key="cnstrct", name="CNSTRCT")
        client = TestClient(create_app(database_path=db_path))
        response = client.get("/api/v1/applications/cnstrcta")  # one char off
        assert response.status_code == 404
        assert "CNSTRCT" in response.json()["error"]["message"]


class TestApplicationHistory:
    def test_since_parsing_and_chronological_ordering(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed_incident_stack(db_path)

        client = TestClient(create_app(database_path=db_path))
        response = client.get("/api/v1/applications/cnstrct/history?since=1h")
        assert response.status_code == 200
        body = response.json()
        assert body["application"] == "cnstrct"
        assert body["since"] is not None
        timestamps = [t["occurred_at"] for t in body["transitions"]]
        assert timestamps == sorted(timestamps)

    def test_default_since_is_24h(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed_incident_stack(db_path)
        client = TestClient(create_app(database_path=db_path))
        with_default = client.get("/api/v1/applications/cnstrct/history")
        with_explicit = client.get("/api/v1/applications/cnstrct/history?since=24h")
        assert with_default.status_code == with_explicit.status_code == 200
        assert len(with_default.json()["transitions"]) == len(with_explicit.json()["transitions"])

    def test_invalid_since_is_a_clean_422(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed_incident_stack(db_path)
        client = TestClient(create_app(database_path=db_path))
        response = client.get("/api/v1/applications/cnstrct/history?since=notaduration")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_query_parameter"

    def test_history_for_unknown_application_is_404(self, tmp_path):
        client = TestClient(create_app(database_path=tmp_path / "a.db"))
        response = client.get("/api/v1/applications/nope/history")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "application_not_found"
