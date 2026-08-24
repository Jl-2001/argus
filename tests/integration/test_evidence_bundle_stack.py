"""Milestone 11 -- end-to-end evidence bundle assembly against the real,
disposable `argus-test-stack`. Produces a real incident with real
db_connection_timeout / authentication_failure evidence and a real
health transition, then assembles a bundle and verifies it against the
real SQLite database and the real CLI.

Same safety discipline as test_chaos_stack.py / test_evidence_stack.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from argus.cli.main import main as cli_main
from argus.collector.loop import CollectorLoop
from argus.collectors.docker_client import DockerClient
from argus.evidence.assembler import DEFAULT_ASSEMBLER_CONFIG, assemble_evidence_bundle

from conftest import TEST_PROJECT_NAME, safe_stop, safe_start, wait_until, compose_container_id
from test_chaos_stack import TEST_CONFIG, TEST_RULES, APPLICATION_FAILURE_SIGNATURE, _argus_test_stack_is_healthy
from test_evidence_stack import _RAW_JWT_SUBSTRING, _RAW_JWT_HEADER

pytestmark = [pytest.mark.integration, pytest.mark.docker]


class TestRealEvidenceBundle:
    def test_bundle_assembled_from_a_real_incident_with_real_evidence(
        self, stack, raw_docker, argus_db, log_emitter, capsys
    ):
        db_path, connection, repository = argus_db
        client = DockerClient()
        loop = CollectorLoop(client=client, repository=repository, config=TEST_CONFIG, rules=TEST_RULES)

        # 1. Real evidence: wait for log-emitter's known lines to be
        # collected and aggregated.
        def evidence_ready():
            tick = loop.run_once()
            assert tick.success
            app = repository.get_application(TEST_PROJECT_NAME)
            if app is None:
                return False
            signals = repository.list_log_signals_for_application(
                app.id, since=datetime.now(timezone.utc) - timedelta(hours=1)
            )
            categories = {s.category.value for s in signals}
            return {"db_connection_timeout", "authentication_failure"} <= categories

        wait_until(evidence_ready, timeout=30, interval=1, description="real db_connection_timeout + authentication_failure evidence collected")

        # 2. A real incident: stop healthy-api.
        container_id = compose_container_id("healthy-api")
        incident_id = None
        try:
            safe_stop(raw_docker, container_id)
            wait_until(
                lambda: raw_docker.containers.get(container_id).status == "exited",
                timeout=20, interval=1, description="healthy-api exited",
            )

            tick = loop.run_once()
            assert tick.success

            open_incident = repository.get_open_incident(failure_signature=APPLICATION_FAILURE_SIGNATURE)
            assert open_incident is not None
            incident_id = open_incident.id

            # 3. Assemble the bundle.
            now = datetime.now(timezone.utc)
            bundle = assemble_evidence_bundle(repository, incident_id, now=now, config=DEFAULT_ASSEMBLER_CONFIG)

            # -- incident metadata correct --
            assert bundle.incident.incident_id == incident_id
            assert bundle.incident.status == "open"
            assert bundle.application.key == TEST_PROJECT_NAME

            # -- real evidence included --
            categories = {s.category for s in bundle.signals}
            assert "db_connection_timeout" in categories or "authentication_failure" in categories

            # -- references valid against SQLite --
            for signal in bundle.signals:
                assert repository.get_log_signal(signal.source_id) is not None
            for transition in bundle.transitions:
                row = connection.execute(
                    "SELECT id FROM health_transitions WHERE id = ?", (transition.source_id,)
                ).fetchone()
                assert row is not None

            # -- all samples redacted --
            raw = bundle.to_json()
            assert _RAW_JWT_SUBSTRING not in raw
            assert _RAW_JWT_HEADER not in raw

            # -- bundle under size limit --
            assert len(bundle.to_json(indent=None)) <= DEFAULT_ASSEMBLER_CONFIG.max_total_chars

            # -- fingerprint present --
            assert bundle.metadata.fingerprint
            assert len(bundle.metadata.fingerprint) == 64  # sha256 hex digest

            # -- CLI JSON parseable --
            code = cli_main(["--database", str(db_path), "bundle", str(incident_id), "--json"])
            cli_raw = capsys.readouterr().out
            assert code == 0
            cli_payload = json.loads(cli_raw)
            assert cli_payload["incident"]["incident_id"] == incident_id
            assert _RAW_JWT_SUBSTRING not in cli_raw
        finally:
            safe_start(raw_docker, container_id)
            wait_until(
                lambda: _argus_test_stack_is_healthy(DockerClient()),
                timeout=30, interval=2, description="argus-test-stack healthy before next test",
                on_timeout=lambda: "leaving healthy-api in whatever state it is in -- see the failure above",
            )
