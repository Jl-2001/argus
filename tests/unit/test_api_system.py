"""Milestone 13 -- GET /api/v1/system/status and GET /api/v1/system/doctor.

`TestStatusMatchesCLI` is a CLI-parity test: it runs `argus status
--json` against the exact same database and asserts the API's JSON
carries the same facts -- proving both surfaces really do share
`argus.cli.queries.get_collector_status`/`list_application_summaries`
rather than recomputing "current status" a second way.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

from fastapi.testclient import TestClient

from api_fixtures import seed_incident_stack
from argus.api.app import create_app
from argus.cli import main as main_module


def run_cli_json(db_path, *args: str) -> dict:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main_module.main(["--database", str(db_path), *args])
    assert code == 0
    return json.loads(buffer.getvalue())


class TestStatusEndpoint:
    def test_status_shape_on_empty_database(self, tmp_path):
        client = TestClient(create_app(database_path=tmp_path / "a.db"))
        body = client.get("/api/v1/system/status").json()
        assert set(body.keys()) == {"collector", "applications", "open_incidents"}
        assert set(body["collector"].keys()) == {
            "status", "last_tick_at", "last_success_at", "consecutive_failures", "last_error",
        }

    def test_status_reflects_seeded_application_and_open_incident(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed_incident_stack(db_path)

        client = TestClient(create_app(database_path=db_path))
        body = client.get("/api/v1/system/status").json()

        assert body["open_incidents"] == 1
        assert len(body["applications"]) == 1
        app = body["applications"][0]
        assert app["key"] == "cnstrct"
        assert app["status"] == "UNHEALTHY"
        assert app["services"] == 1
        assert app["containers"] == 1


class TestStatusMatchesCLI:
    def test_api_status_matches_argus_status_json(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed_incident_stack(db_path)

        cli_payload = run_cli_json(db_path, "status", "--json")
        client = TestClient(create_app(database_path=db_path))
        api_payload = client.get("/api/v1/system/status").json()

        # Same collector classification, same application count/status,
        # same open-incident count -- not two independent definitions of
        # "current status" that happen to usually agree.
        assert api_payload["collector"]["status"] == cli_payload["collector"]["status"]
        assert api_payload["open_incidents"] == cli_payload["open_incidents"]
        assert [a["key"] for a in api_payload["applications"]] == [a["key"] for a in cli_payload["applications"]]
        assert [a["status"] for a in api_payload["applications"]] == [a["status"] for a in cli_payload["applications"]]


class TestDoctorEndpoint:
    def test_doctor_reports_missing_database_before_anything_else_touches_it(self, tmp_path):
        client = TestClient(create_app(database_path=tmp_path / "never-created.db"))
        response = client.get("/api/v1/system/doctor")
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body["operational"], bool)
        names = {check["name"] for check in body["checks"]}
        assert names == {
            "configuration", "database", "docker_connection", "docker_read_access",
            "collector_heartbeat", "clock",
        }
        db_check = next(c for c in body["checks"] if c["name"] == "database")
        assert db_check["status"] == "FAIL"

    def test_doctor_check_status_values_are_plain_strings(self, tmp_path):
        client = TestClient(create_app(database_path=tmp_path / "a.db"))
        body = client.get("/api/v1/system/doctor").json()
        for check in body["checks"]:
            assert check["status"] in ("PASS", "WARN", "FAIL", "SKIP")
