"""Milestone 10 -- end-to-end evidence collection against the real,
disposable `argus-test-stack` (see tests/docker/docker-compose.test.yml's
`log-emitter` service and conftest.py's `log_emitter` fixture).

Same safety discipline as test_chaos_stack.py: every test here is
`@pytest.mark.integration`/`@pytest.mark.docker`, every mutation goes
through conftest.py's `safe_*` guard, and `host_preservation_check`
(pulled in transitively via `stack`) still backstops the whole file.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from argus.cli.main import main as cli_main
from argus.collector.loop import CollectorLoop
from argus.collectors.docker_client import DockerClient
from argus.domain.models import HealthStatus
from argus.store.database import open_database
from argus.store.repository import Repository

from conftest import TEST_PROJECT_NAME, safe_stop, safe_start, wait_until, compose_container_id
from test_chaos_stack import TEST_CONFIG, TEST_RULES, APPLICATION_FAILURE_SIGNATURE, _argus_test_stack_is_healthy

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.docker]

# The raw secret substrings the log-emitter's own log lines genuinely
# contain -- none of these may ever appear anywhere in the database or
# in CLI output.
_RAW_JWT_SUBSTRING = "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PYVsLquNPYJI"
_RAW_JWT_HEADER = "eyJhbGciOiJIUzI1NiJ9"


class TestRealLogEvidenceCollection:
    def test_repeated_log_lines_aggregate_and_secrets_are_redacted(
        self, stack, raw_docker, argus_db, log_emitter
    ):
        db_path, connection, repository = argus_db
        client = DockerClient()
        loop = CollectorLoop(client=client, repository=repository, config=TEST_CONFIG, rules=TEST_RULES)
        container_id = log_emitter

        def db_timeout_signal_complete():
            tick = loop.run_once()
            assert tick.success
            app = repository.get_application(TEST_PROJECT_NAME)
            if app is None:
                return False
            signals = repository.list_log_signals_for_application(
                app.id, since=datetime.now(timezone.utc) - timedelta(hours=1)
            )
            by_category = {s.category.value: s for s in signals if s.container_id == container_id}
            # Also wait for the authentication-failure line -- it's
            # emitted by log-emitter a full second *after* the third
            # timeout line, so waiting on the timeout signal alone can
            # satisfy this predicate and stop polling before that later
            # line has even been written yet.
            timeout_signal = by_category.get("db_connection_timeout")
            return (
                timeout_signal is not None and timeout_signal.count >= 3
                and "authentication_failure" in by_category
            )

        wait_until(
            db_timeout_signal_complete, timeout=30, interval=1,
            description="log-emitter's db_connection_timeout and authentication_failure signals both collected",
        )

        app = repository.get_application(TEST_PROJECT_NAME)
        signals = repository.list_log_signals_for_application(app.id, since=datetime.now(timezone.utc) - timedelta(hours=1))
        by_category = {s.category.value: s for s in signals if s.container_id == container_id}

        # the plain startup line never became evidence at all
        assert "db_connection_timeout" in by_category
        assert by_category["db_connection_timeout"].count == 3

        # the authentication-failure line (which carries the fake Bearer
        # token) did become evidence, and its stored sample is redacted
        auth_signal = by_category.get("authentication_failure")
        assert auth_signal is not None
        assert _RAW_JWT_SUBSTRING not in auth_signal.sample
        assert _RAW_JWT_HEADER not in auth_signal.sample
        assert "[REDACTED]" in auth_signal.sample

        # defense in depth: scan the raw SQLite file's own evidence text
        # directly, not just through the Python object
        raw_samples = connection.execute("SELECT sample FROM log_signals").fetchall()
        for row in raw_samples:
            assert _RAW_JWT_SUBSTRING not in row["sample"]
            assert _RAW_JWT_HEADER not in row["sample"]

    def test_cursor_prevents_reingestion_on_a_later_tick(self, stack, raw_docker, argus_db, log_emitter):
        db_path, connection, repository = argus_db
        client = DockerClient()
        loop = CollectorLoop(client=client, repository=repository, config=TEST_CONFIG, rules=TEST_RULES)
        container_id = log_emitter

        def signal_ready():
            tick = loop.run_once()
            assert tick.success
            container_row = repository.get_container_by_docker_id(container_id)
            if container_row is None:
                return False
            signal = repository.find_latest_log_signal(
                container_row_id=container_row.id, category="db_connection_timeout",
                normalized_signature="database connection timeout",
            )
            return signal is not None and signal.count >= 3

        wait_until(signal_ready, timeout=30, interval=1, description="db_connection_timeout signal reaches count 3")

        container_row = repository.get_container_by_docker_id(container_id)
        signal_before = repository.find_latest_log_signal(
            container_row_id=container_row.id, category="db_connection_timeout",
            normalized_signature="database connection timeout",
        )

        # log-emitter has already emitted everything and is now just
        # sleeping -- a further tick must not re-count the same lines.
        second_tick = loop.run_once()
        assert second_tick.success
        signal_after = repository.find_latest_log_signal(
            container_row_id=container_row.id, category="db_connection_timeout",
            normalized_signature="database connection timeout",
        )
        assert signal_after.count == signal_before.count


class TestRealCliEvidenceEndToEnd:
    def test_cli_evidence_json_never_leaks_the_raw_secret(self, stack, argus_db, log_emitter, capsys):
        db_path, connection, repository = argus_db
        client = DockerClient()
        loop = CollectorLoop(client=client, repository=repository, config=TEST_CONFIG, rules=TEST_RULES)

        def has_evidence():
            tick = loop.run_once()
            assert tick.success
            app = repository.get_application(TEST_PROJECT_NAME)
            if app is None:
                return False
            signals = repository.list_log_signals_for_application(
                app.id, since=datetime.now(timezone.utc) - timedelta(hours=1)
            )
            return len(signals) >= 2

        wait_until(has_evidence, timeout=30, interval=1, description="at least 2 evidence signals collected")

        code = cli_main(["--database", str(db_path), "evidence", TEST_PROJECT_NAME, "--json"])
        raw = capsys.readouterr().out
        assert code == 0
        payload = json.loads(raw)
        assert len(payload["evidence"]) >= 2

        assert _RAW_JWT_SUBSTRING not in raw
        assert _RAW_JWT_HEADER not in raw
        for forbidden in ("labels", "Env", "com.docker.compose"):
            assert forbidden not in raw

        code = cli_main(["--database", str(db_path), "evidence", TEST_PROJECT_NAME])
        human_out = capsys.readouterr().out
        assert code == 0
        assert _RAW_JWT_SUBSTRING not in human_out


class TestRealIncidentAssociation:
    def test_evidence_from_one_service_links_to_an_incident_opened_by_another(
        self, stack, raw_docker, argus_db, log_emitter
    ):
        """log-emitter and healthy-api are different services in the same
        application -- evidence collected from one is still eligible to
        associate with an incident whose status change came from the
        other, since association is application-scoped, not
        service-scoped (mirroring the milestone's own worked example:
        an incident's evidence can span multiple services)."""

        db_path, connection, repository = argus_db
        client = DockerClient()
        loop = CollectorLoop(client=client, repository=repository, config=TEST_CONFIG, rules=TEST_RULES)

        def evidence_ready():
            tick = loop.run_once()
            assert tick.success
            app = repository.get_application(TEST_PROJECT_NAME)
            if app is None:
                return False
            signals = repository.list_log_signals_for_application(
                app.id, since=datetime.now(timezone.utc) - timedelta(hours=1)
            )
            return len(signals) >= 1

        wait_until(evidence_ready, timeout=30, interval=1, description="at least one evidence signal collected")

        container_id = compose_container_id("healthy-api")
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
            linked = repository.list_evidence_for_incident(open_incident.id)
            assert len(linked) >= 1
        finally:
            safe_start(raw_docker, container_id)
            wait_until(
                lambda: _argus_test_stack_is_healthy(DockerClient()),
                timeout=30, interval=2, description="argus-test-stack healthy before next test",
                on_timeout=lambda: "leaving healthy-api in whatever state it is in -- see the failure above",
            )
