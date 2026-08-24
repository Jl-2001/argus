"""End-to-end tests for `argus bundle`: invoke argus.cli.main.main()
with argv, assert on captured stdout/stderr and exit codes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from argus.cli import main as main_module
from argus.domain.models import HealthStatus
from argus.store.database import open_database
from argus.store.repository import Repository

UTC = timezone.utc
NOW = datetime.now(UTC)


def run_cli(db_path: Path, *args: str) -> int:
    return main_module.main(["--database", str(db_path), *args])


def seed_incident(tmp_path, *, with_signal=True):
    db_path = tmp_path / "a.db"
    conn = open_database(db_path)
    repo = Repository(conn)
    app_id = repo.upsert_application(key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=NOW - timedelta(minutes=5))
    svc_id = repo.upsert_service(application_id=app_id, compose_service="api", name="api", observed_at=NOW - timedelta(minutes=5))
    container_row_id = repo.upsert_container(
        service_id=svc_id, container_id="docker-api", name="cnstrct-api-1",
        first_seen_at=NOW - timedelta(minutes=5), last_seen_at=NOW - timedelta(minutes=5),
    )
    t = repo.insert_transition(
        scope="application", scope_id=app_id, from_status=None, to_status=HealthStatus.UNHEALTHY,
        occurred_at=NOW - timedelta(minutes=1),
    )
    incident_id = repo.open_incident(
        scope_id=app_id, failure_signature="application:cnstrct", opened_at=NOW - timedelta(minutes=1),
        opening_status=HealthStatus.UNHEALTHY, opening_transition_id=t,
    )
    if with_signal:
        sig_id = repo.insert_log_signal(
            application_id=app_id, container_row_id=container_row_id, category="db_connection_timeout",
            severity="high", normalized_signature="timeout", first_seen_at=NOW - timedelta(minutes=1),
            last_seen_at=NOW - timedelta(minutes=1), count=27, sample="connection timeout after 30s",
            source_type="container_log", source_ref="stdout+stderr",
        )
        repo.link_incident_evidence(incident_id=incident_id, log_signal_id=sig_id, linked_at=NOW - timedelta(minutes=1))
    conn.close()
    return db_path, incident_id


class TestHumanSummary:
    def test_summary_shows_required_fields(self, tmp_path, capsys):
        db_path, incident_id = seed_incident(tmp_path)
        code = run_cli(db_path, "bundle", str(incident_id))
        out = capsys.readouterr().out
        assert code == 0
        assert f"Incident #{incident_id}" in out
        assert "Application CNSTRCT" in out
        assert "Signals       1" in out
        assert "Transitions" in out
        assert "Observations" in out
        assert "Truncated     no" in out
        assert "Fingerprint" in out

    def test_default_human_mode_does_not_dump_the_timeline(self, tmp_path, capsys):
        db_path, incident_id = seed_incident(tmp_path)
        run_cli(db_path, "bundle", str(incident_id))
        out = capsys.readouterr().out
        assert "db_connection_timeout" not in out  # only visible via --full or --json

    def test_full_flag_prints_the_timeline(self, tmp_path, capsys):
        db_path, incident_id = seed_incident(tmp_path)
        run_cli(db_path, "bundle", str(incident_id), "--full")
        out = capsys.readouterr().out
        assert "db_connection_timeout" in out
        assert "TIME" in out and "TYPE" in out and "ENTITY" in out and "FACTS" in out and "REFERENCE" in out


class TestJsonOutput:
    def test_json_is_parseable_and_matches_bundle_shape(self, tmp_path, capsys):
        db_path, incident_id = seed_incident(tmp_path)
        run_cli(db_path, "bundle", str(incident_id), "--json")
        payload = json.loads(capsys.readouterr().out)
        assert payload["incident"]["incident_id"] == incident_id
        assert payload["application"]["key"] == "cnstrct"
        assert len(payload["signals"]) == 1
        assert payload["signals"][0]["reference"].startswith("log_signal:")
        assert "fingerprint" in payload["metadata"]

    def test_json_never_leaks_raw_labels_or_env(self, tmp_path, capsys):
        db_path, incident_id = seed_incident(tmp_path)
        run_cli(db_path, "bundle", str(incident_id), "--json")
        raw = capsys.readouterr().out
        for forbidden in ("labels", "Env", "com.docker.compose"):
            assert forbidden not in raw


class TestNonexistentIncident:
    def test_exits_1_with_clear_message_no_traceback(self, tmp_path, capsys):
        db_path, _ = seed_incident(tmp_path)
        code = run_cli(db_path, "bundle", "999999")
        captured = capsys.readouterr()
        assert code == 1
        assert "999999" in captured.err
        assert "Traceback" not in captured.err


class TestNoEvidenceIncident:
    def test_empty_signals_still_produces_a_valid_summary(self, tmp_path, capsys):
        db_path, incident_id = seed_incident(tmp_path, with_signal=False)
        code = run_cli(db_path, "bundle", str(incident_id))
        out = capsys.readouterr().out
        assert code == 0
        assert "Signals       0" in out


class TestHelp:
    def test_bundle_listed_in_top_level_help(self, capsys):
        import pytest

        with pytest.raises(SystemExit) as exc_info:
            main_module.main(["--help"])
        assert exc_info.value.code == 0
        assert "bundle" in capsys.readouterr().out

    def test_bundle_help_lists_flags(self, capsys):
        import pytest

        with pytest.raises(SystemExit) as exc_info:
            main_module.main(["bundle", "--help"])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "--json" in out
        assert "--full" in out
