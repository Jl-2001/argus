"""End-to-end tests for `argus evidence`: invoke argus.cli.main.main()
with argv, assert on captured stdout/stderr and exit codes. Populates a
temporary database directly through Repository -- no Docker, no
collector process running.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from argus.cli import main as main_module
from argus.domain.models import HealthStatus
from argus.store.database import open_database
from argus.store.repository import Repository

UTC = timezone.utc
NOW = datetime.now(UTC)  # `main.main()` reads the real clock -- see test_cli_commands.py's own REAL_NOW note


def run_cli(db_path: Path, *args: str) -> int:
    return main_module.main(["--database", str(db_path), *args])


def seed_application(repo, *, key="cnstrct", name="CNSTRCT", at=NOW):
    app_id = repo.upsert_application(key=key, name=name, is_standalone=False, observed_at=at)
    svc_id = repo.upsert_service(application_id=app_id, compose_service="api", name="api", observed_at=at)
    container_row_id = repo.upsert_container(
        service_id=svc_id, container_id="docker-api", name=f"{key}-api-1", first_seen_at=at, last_seen_at=at
    )
    return app_id, container_row_id


def seed_signal(repo, *, app_id, container_row_id, at, category="db_connection_timeout", severity="high", count=27, sample="connection timeout after 30s", source_type="container_log", source_ref="stdout+stderr"):
    return repo.insert_log_signal(
        application_id=app_id, container_row_id=container_row_id, category=category, severity=severity,
        normalized_signature=sample, first_seen_at=at, last_seen_at=at, count=count, sample=sample,
        source_type=source_type, source_ref=source_ref,
    )


class TestEvidenceForApplication:
    def test_table_shows_expected_columns(self, tmp_path, capsys):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        app_id, container_row_id = seed_application(repo)
        seed_signal(repo, app_id=app_id, container_row_id=container_row_id, at=NOW - timedelta(minutes=5))
        conn.close()

        code = run_cli(db_path, "evidence", "CNSTRCT")
        out = capsys.readouterr().out
        assert code == 0
        assert "TIME" in out and "SEVERITY" in out and "CATEGORY" in out and "COUNT" in out and "SOURCE" in out
        assert "db_connection_timeout" in out
        assert "high" in out
        assert "27" in out
        assert "api" in out

    def test_source_label_resolves_to_compose_service_name(self, tmp_path, capsys):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        app_id, container_row_id = seed_application(repo)
        seed_signal(repo, app_id=app_id, container_row_id=container_row_id, at=NOW - timedelta(minutes=1))
        conn.close()

        run_cli(db_path, "evidence", "CNSTRCT")
        out = capsys.readouterr().out
        assert "docker-api" not in out  # the raw Docker id must never leak into the human table
        assert "api" in out

    def test_json_is_parseable_and_sanitized(self, tmp_path, capsys):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        app_id, container_row_id = seed_application(repo)
        seed_signal(
            repo, app_id=app_id, container_row_id=container_row_id, at=NOW - timedelta(minutes=1),
            sample="Authorization: [REDACTED] failed",
        )
        conn.close()

        run_cli(db_path, "evidence", "CNSTRCT", "--json")
        raw = capsys.readouterr().out
        payload = json.loads(raw)
        assert payload["application"] == "CNSTRCT"
        assert len(payload["evidence"]) == 1
        assert payload["evidence"][0]["category"] == "db_connection_timeout"
        assert "labels" not in raw
        assert "Env" not in raw

    def test_empty_window_produces_friendly_message(self, tmp_path, capsys):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        seed_application(repo)
        conn.close()

        code = run_cli(db_path, "evidence", "CNSTRCT")
        out = capsys.readouterr().out
        assert code == 0
        assert "No evidence recorded in this window." in out

    def test_since_filters_out_older_evidence(self, tmp_path, capsys):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        app_id, container_row_id = seed_application(repo)
        seed_signal(repo, app_id=app_id, container_row_id=container_row_id, at=NOW - timedelta(hours=2))
        conn.close()

        code = run_cli(db_path, "evidence", "CNSTRCT", "--since", "30m")
        out = capsys.readouterr().out
        assert code == 0
        assert "No evidence recorded in this window." in out

    def test_application_not_found_suggests_a_name(self, tmp_path, capsys):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        seed_application(repo)
        conn.close()

        code = run_cli(db_path, "evidence", "cnstrt")
        captured = capsys.readouterr()
        assert code == 1
        assert "not found" in captured.err
        assert "Did you mean 'CNSTRCT'?" in captured.err

    def test_invalid_since_is_argument_error_exit_2(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            run_cli(tmp_path / "a.db", "evidence", "CNSTRCT", "--since", "yesterday-ish")
        assert exc_info.value.code == 2


class TestEvidenceForIncident:
    def _seed_with_incident(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        app_id, container_row_id = seed_application(repo)
        signal_id = seed_signal(repo, app_id=app_id, container_row_id=container_row_id, at=NOW - timedelta(minutes=1))
        t = repo.insert_transition(
            scope="application", scope_id=app_id, from_status=None, to_status=HealthStatus.UNHEALTHY,
            occurred_at=NOW - timedelta(minutes=1),
        )
        incident_id = repo.open_incident(
            scope_id=app_id, failure_signature="application:cnstrct", opened_at=NOW - timedelta(minutes=1),
            opening_status=HealthStatus.UNHEALTHY, opening_transition_id=t,
        )
        repo.link_incident_evidence(incident_id=incident_id, log_signal_id=signal_id, linked_at=NOW)
        conn.close()
        return db_path, incident_id

    def test_incident_detail_shows_required_fields(self, tmp_path, capsys):
        db_path, incident_id = self._seed_with_incident(tmp_path)
        code = run_cli(db_path, "evidence", "CNSTRCT", "--incident", str(incident_id))
        out = capsys.readouterr().out
        assert code == 0
        assert f"Evidence for incident #{incident_id}" in out
        assert "db_connection_timeout" in out
        assert "First seen" in out
        assert "Last seen" in out
        assert "Source" in out
        assert "Sample" in out
        assert "connection timeout after 30s" in out

    def test_incident_json(self, tmp_path, capsys):
        db_path, incident_id = self._seed_with_incident(tmp_path)
        run_cli(db_path, "evidence", "CNSTRCT", "--incident", str(incident_id), "--json")
        payload = json.loads(capsys.readouterr().out)
        assert payload["incident"] == incident_id
        assert len(payload["evidence"]) == 1

    def test_unknown_incident_id_exits_1(self, tmp_path, capsys):
        db_path, _ = self._seed_with_incident(tmp_path)
        code = run_cli(db_path, "evidence", "CNSTRCT", "--incident", "999999")
        captured = capsys.readouterr()
        assert code == 1
        assert "not found" in captured.err

    def test_incident_belonging_to_a_different_application_is_rejected(self, tmp_path, capsys):
        db_path, incident_id = self._seed_with_incident(tmp_path)
        conn = open_database(db_path)
        repo = Repository(conn)
        repo.upsert_application(key="other-app", name="OtherApp", is_standalone=False, observed_at=NOW)
        conn.close()

        code = run_cli(db_path, "evidence", "OtherApp", "--incident", str(incident_id))
        captured = capsys.readouterr()
        assert code == 1
        assert "does not belong to application" in captured.err

    def test_incident_with_no_linked_evidence_shows_friendly_message(self, tmp_path, capsys):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        app_id, _ = seed_application(repo)
        t = repo.insert_transition(
            scope="application", scope_id=app_id, from_status=None, to_status=HealthStatus.UNHEALTHY, occurred_at=NOW
        )
        incident_id = repo.open_incident(
            scope_id=app_id, failure_signature="application:cnstrct", opened_at=NOW,
            opening_status=HealthStatus.UNHEALTHY, opening_transition_id=t,
        )
        conn.close()

        code = run_cli(db_path, "evidence", "CNSTRCT", "--incident", str(incident_id))
        out = capsys.readouterr().out
        assert code == 0
        assert f"No evidence linked to incident #{incident_id}." in out


class TestNoRootCauseLanguage:
    def test_output_never_uses_causal_language(self, tmp_path, capsys):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        app_id, container_row_id = seed_application(repo)
        seed_signal(repo, app_id=app_id, container_row_id=container_row_id, at=NOW - timedelta(minutes=1))
        conn.close()

        run_cli(db_path, "evidence", "CNSTRCT")
        out = capsys.readouterr().out
        for forbidden_phrase in ("caused", "root cause", "because of", "due to"):
            assert forbidden_phrase not in out.lower()
