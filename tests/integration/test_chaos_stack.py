"""Milestone 9 -- end-to-end / chaos tests against a real, disposable
Docker stack (`argus-test-stack`, see tests/docker/docker-compose.test.yml).

Every test here talks to a real Docker daemon (`@pytest.mark.docker`) and
is part of the integration suite (`@pytest.mark.integration`); neither is
collected by `pytest -m "not integration"`. Nothing in this file (or in
conftest.py) mutates any container outside `argus-test-stack` -- every
mutation goes through conftest.py's `safe_stop`/`safe_start`/`safe_restart`,
which refuse anything not carrying the exact
`com.docker.compose.project == "argus-test-stack"` label. The
`host_preservation_check` session fixture (pulled in transitively by
`stack`) is the hard backstop: it snapshots every non-test container on
this host before this file's tests run and asserts nothing about them
changed, however this file's tests behave.

Scenario numbering below matches the Milestone 9 specification exactly.
Scenario 10 (empty selection) is deliberately the first class in this
file and requests no `stack` fixture, so it runs before the module-scoped
`stack` fixture starts anything -- pytest runs a module's tests in
declaration order by default, and nothing here changes that.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import docker.errors
import pytest

import argus.collector.loop as loop_module
from argus.cli.main import main as cli_main
from argus.collector.loop import CollectorConfig, CollectorLoop
from argus.collectors.docker_client import DockerClient
from argus.collectors.docker_collector import discover
from argus.domain.health import DEFAULT_HEALTH_RULES, HealthRules
from argus.domain.models import HealthStatus
from argus.incidents.engine import IncidentProcessingError
from argus.store.database import open_database
from argus.store.repository import Repository

from conftest import (
    TEST_PROJECT_NAME,
    TEST_POLL_INTERVAL,
    TEST_UNKNOWN_AFTER,
    TEST_RESTART_LOOP_WINDOW,
    TEST_RESTART_LOOP_THRESHOLD,
    UnsafeMutationError,
    assert_safe_to_mutate,
    safe_stop,
    safe_start,
    safe_restart,
    wait_until,
    compose_container_id,
    snapshot_non_test_containers,
)

pytestmark = [pytest.mark.integration, pytest.mark.docker]

# Faster-than-production rules/config for this whole file -- never used
# outside tests/integration/. See conftest.py for why each number was
# chosen.
TEST_RULES = HealthRules(
    unknown_after=TEST_UNKNOWN_AFTER,
    restart_loop_window=TEST_RESTART_LOOP_WINDOW,
    restart_loop_threshold=TEST_RESTART_LOOP_THRESHOLD,
    degraded_restart_threshold=1,
)
TEST_CONFIG = CollectorConfig(poll_interval=TEST_POLL_INTERVAL, backoff_initial=1.0, backoff_max=4.0)

APPLICATION_FAILURE_SIGNATURE = f"application:{TEST_PROJECT_NAME}"


def _argus_test_stack_is_healthy(client: DockerClient) -> bool:
    """A fresh, one-shot discovery pass's opinion of argus-test-stack's
    own rollup status -- used by several scenarios' recovery waits."""

    result = discover(client, observed_at=datetime.now(timezone.utc), rules=TEST_RULES)
    matches = [app for app in result.applications if app.key == TEST_PROJECT_NAME]
    return bool(matches) and matches[0].derived_status is HealthStatus.HEALTHY


# ==========================================================================
# Scenario 10 -- empty selection (runs before `stack` starts anything)
# ==========================================================================


class TestScenario10EmptySelection:
    def test_argus_test_stack_absent_before_the_stack_exists(self):
        client = DockerClient()
        result = discover(client, observed_at=datetime.now(timezone.utc), rules=TEST_RULES)
        keys = {app.key for app in result.applications}
        assert TEST_PROJECT_NAME not in keys


# ==========================================================================
# Scenario 1 -- baseline discovery
# ==========================================================================


class TestScenario1BaselineDiscovery:
    def test_argus_test_stack_discovered_exactly_once_and_healthy(self, stack):
        client = DockerClient()
        result = discover(client, observed_at=datetime.now(timezone.utc), rules=TEST_RULES)

        # Never assume argus-test-stack is the *only* application on this
        # daemon -- only that it appears, and appears exactly once.
        matches = [app for app in result.applications if app.key == TEST_PROJECT_NAME]
        assert len(matches) == 1

        app = matches[0]
        assert app.derived_status is HealthStatus.HEALTHY
        assert {service.compose_service for service in app.services} == {
            "healthy-api", "redis", "postgres",
        }


# ==========================================================================
# Scenario 2 -- stop/start with incident open + resolve
# ==========================================================================


class TestScenario2StopStartIncidentLifecycle:
    def test_stopping_and_restarting_a_container_opens_then_resolves_an_incident(
        self, stack, raw_docker, argus_db
    ):
        db_path, connection, repository = argus_db
        client = DockerClient()
        loop = CollectorLoop(client=client, repository=repository, config=TEST_CONFIG, rules=TEST_RULES)

        baseline = loop.run_once()
        assert baseline.success
        assert repository.get_open_incident(failure_signature=APPLICATION_FAILURE_SIGNATURE) is None

        container_id = compose_container_id("healthy-api")
        try:
            safe_stop(raw_docker, container_id)
            wait_until(
                lambda: raw_docker.containers.get(container_id).status == "exited",
                timeout=20, interval=1, description="healthy-api reports exited",
            )

            stopped_tick = loop.run_once()
            assert stopped_tick.success

            open_incident = repository.get_open_incident(failure_signature=APPLICATION_FAILURE_SIGNATURE)
            assert open_incident is not None
            # a single stopped service among three others is a *partial*
            # stop -- application rollup reports that as UNHEALTHY, not
            # STOPPED (see argus.domain.health.evaluate_application_health).
            assert open_incident.opening_status is HealthStatus.UNHEALTHY

            safe_start(raw_docker, container_id)
            wait_until(
                lambda: _argus_test_stack_is_healthy(client),
                timeout=30, interval=2, description="argus-test-stack fully healthy again",
            )

            recovered_tick = loop.run_once()
            assert recovered_tick.success

            assert repository.get_open_incident(failure_signature=APPLICATION_FAILURE_SIGNATURE) is None
            resolved = repository.get_incident_by_id(open_incident.id)
            assert resolved.status == "resolved"
            assert resolved.closed_at is not None
        finally:
            # Best-effort: make sure healthy-api is left running for whatever
            # test runs next in this module, even if an assertion above failed.
            wait_until(
                lambda: _argus_test_stack_is_healthy(DockerClient()),
                timeout=30, interval=2, description="argus-test-stack healthy before next test",
                on_timeout=lambda: "leaving healthy-api in whatever state it is in -- see the failure above",
            )


# ==========================================================================
# Scenario 3 -- crash loop via real restart_count
# ==========================================================================


class TestScenario3CrashLoopRealRestartCount:
    def test_intentional_failure_service_reaches_restarting_via_real_restart_count(
        self, stack, raw_docker, argus_db, failure_service
    ):
        db_path, connection, repository = argus_db
        client = DockerClient()
        loop = CollectorLoop(client=client, repository=repository, config=TEST_CONFIG, rules=TEST_RULES)
        container_id = failure_service

        samples_seen: list[tuple[str, int]] = []

        def reached_restart_loop_threshold() -> bool:
            tick = loop.run_once()
            assert tick.success
            observation = repository.get_latest_observation(container_id)
            if observation is None:
                return False
            samples_seen.append((observation.derived_status.value, observation.restart_count))
            # Docker's own live "restarting" docker_state can classify a
            # container RESTARTING immediately, well before restart_count
            # itself has climbed far -- waiting on status alone would prove
            # only that clause, not "via real restart_count" specifically.
            # This waits for the real RestartCount field to actually cross
            # the threshold (and still be classified RESTARTING then).
            return (
                observation.restart_count >= TEST_RESTART_LOOP_THRESHOLD
                and observation.derived_status is HealthStatus.RESTARTING
            )

        wait_until(
            reached_restart_loop_threshold,
            timeout=90, interval=TEST_POLL_INTERVAL,
            description="intentional-failure-service's real restart_count crosses the "
            "restart-loop threshold while classified RESTARTING",
            on_timeout=lambda: f"(status, restart_count) samples observed: {samples_seen}",
        )

        final_observation = repository.get_latest_observation(container_id)
        assert final_observation.restart_count >= TEST_RESTART_LOOP_THRESHOLD

        # One application-level incident, however many ticks/restarts it took
        # to get there -- no duplicate opened for the same failure signature.
        open_incident = repository.get_open_incident(failure_signature=APPLICATION_FAILURE_SIGNATURE)
        assert open_incident is not None


# ==========================================================================
# Scenario 4 -- Docker health "starting" window -> DEGRADED
# ==========================================================================


class TestScenario4DockerHealthStartingWindow:
    def test_restart_produces_a_deterministic_starting_window_as_degraded(
        self, stack, raw_docker, argus_db
    ):
        db_path, connection, repository = argus_db
        client = DockerClient()
        loop = CollectorLoop(client=client, repository=repository, config=TEST_CONFIG, rules=TEST_RULES)
        container_id = compose_container_id("healthy-api")

        try:
            safe_restart(raw_docker, container_id)
            wait_until(
                lambda: raw_docker.containers.get(container_id).status == "running",
                timeout=15, interval=0.5, description="healthy-api running again after restart",
            )

            tick = loop.run_once()
            assert tick.success

            observation = repository.get_latest_observation(container_id)
            # healthy-api's healthcheck interval is a deliberately wide 6s
            # (see docker-compose.test.yml's own comment) so a fresh restart
            # is reliably caught here, before Docker's first real healthcheck
            # has even run.
            assert observation.docker_health is not None
            assert observation.docker_health.value == "starting"
            assert observation.derived_status is HealthStatus.DEGRADED
        finally:
            wait_until(
                lambda: _argus_test_stack_is_healthy(DockerClient()),
                timeout=30, interval=2, description="argus-test-stack fully healthy again after restart",
            )


# ==========================================================================
# Scenario 5 -- collector restart safety
# ==========================================================================


class TestScenario5CollectorRestartSafety:
    def test_a_fresh_repository_and_loop_against_the_same_db_does_not_duplicate_state(
        self, stack, raw_docker, argus_db
    ):
        db_path, connection, repository = argus_db
        client = DockerClient()
        loop_a = CollectorLoop(client=client, repository=repository, config=TEST_CONFIG, rules=TEST_RULES)

        baseline = loop_a.run_once()
        assert baseline.success
        assert repository.get_open_incident(failure_signature=APPLICATION_FAILURE_SIGNATURE) is None

        container_id = compose_container_id("healthy-api")
        connection_b = None
        try:
            safe_stop(raw_docker, container_id)
            wait_until(
                lambda: raw_docker.containers.get(container_id).status == "exited",
                timeout=20, interval=1, description="healthy-api exited",
            )

            first_bad_tick = loop_a.run_once()
            assert first_bad_tick.success

            open_incident = repository.get_open_incident(failure_signature=APPLICATION_FAILURE_SIGNATURE)
            assert open_incident is not None
            app_record = repository.get_application(TEST_PROJECT_NAME)
            epoch = datetime(2000, 1, 1, tzinfo=timezone.utc)
            transitions_before = repository.list_transitions_for_application(app_record.id, since=epoch)

            # Simulates a genuine process restart: a brand new Repository and
            # CollectorLoop, pointed at the same database *file*, with
            # nothing at all carried over in memory from loop_a.
            connection_b = open_database(db_path)
            repository_b = Repository(connection_b)
            loop_b = CollectorLoop(
                client=DockerClient(), repository=repository_b, config=TEST_CONFIG, rules=TEST_RULES
            )

            duplicate_tick = loop_b.run_once()
            assert duplicate_tick.success

            still_open = repository.get_open_incident(failure_signature=APPLICATION_FAILURE_SIGNATURE)
            assert still_open is not None
            assert still_open.id == open_incident.id  # not a second, duplicate incident

            transitions_after = repository.list_transitions_for_application(app_record.id, since=epoch)
            # still the same status as before -- no new transition rows from
            # a restarted collector re-observing an unchanged bad state.
            assert len(transitions_after) == len(transitions_before)

            safe_start(raw_docker, container_id)
            wait_until(
                lambda: _argus_test_stack_is_healthy(DockerClient()),
                timeout=30, interval=2, description="argus-test-stack fully healthy again",
            )

            recovered_tick = loop_b.run_once()
            assert recovered_tick.success
            assert repository.get_open_incident(failure_signature=APPLICATION_FAILURE_SIGNATURE) is None
            resolved = repository.get_incident_by_id(open_incident.id)
            assert resolved.status == "resolved"
        finally:
            if connection_b is not None:
                connection_b.close()
            wait_until(
                lambda: _argus_test_stack_is_healthy(DockerClient()),
                timeout=30, interval=2, description="argus-test-stack healthy before next test",
                on_timeout=lambda: "leaving healthy-api in whatever state it is in -- see the failure above",
            )


# ==========================================================================
# Scenario 6 -- mocked Docker-failure simulation, survival + recovery
# ==========================================================================


class _AlwaysFailingContainersAPI:
    def list(self, all=True):
        raise docker.errors.DockerException("simulated docker outage")


class _AlwaysFailingSDKClient:
    def __init__(self) -> None:
        self.containers = _AlwaysFailingContainersAPI()


class TestScenario6MockedDockerFailureSurvival:
    def test_loop_survives_a_docker_outage_then_recovers(self, argus_db):
        """Deliberately independent of the `stack` fixture -- this only
        needs *a* reachable real daemon to prove recovery, not
        argus-test-stack specifically."""

        db_path, connection, repository = argus_db
        failing_client = DockerClient(client=_AlwaysFailingSDKClient())
        loop = CollectorLoop(client=failing_client, repository=repository, config=TEST_CONFIG, rules=TEST_RULES)

        first = loop.run_once()
        second = loop.run_once()

        assert first.success is False
        assert second.success is False
        assert "DockerUnavailableError" in first.error

        state = repository.get_collector_state()
        assert state.last_tick_at is not None       # advances every tick, success or failure
        assert state.last_success_at is None        # never once succeeded
        assert state.consecutive_failures == 2

        recovery_loop = CollectorLoop(
            client=DockerClient(), repository=repository, config=TEST_CONFIG, rules=TEST_RULES
        )
        recovery = recovery_loop.run_once()

        assert recovery.success is True
        state_after = repository.get_collector_state()
        assert state_after.last_success_at is not None
        assert state_after.consecutive_failures == 0


# ==========================================================================
# Scenario 7 -- CLI end-to-end (human-readable)
# ==========================================================================


class TestScenario7CliEndToEnd:
    def test_status_apps_inspect_incidents_history_against_real_docker_derived_data(
        self, stack, argus_db, capsys
    ):
        db_path, connection, repository = argus_db
        client = DockerClient()
        loop = CollectorLoop(client=client, repository=repository, config=TEST_CONFIG, rules=TEST_RULES)
        assert loop.run_once().success

        code = cli_main(["--database", str(db_path), "status"])
        out = capsys.readouterr().out
        assert code == 0
        assert "ARGUS" in out
        assert TEST_PROJECT_NAME in out

        code = cli_main(["--database", str(db_path), "apps"])
        out = capsys.readouterr().out
        assert code == 0
        assert TEST_PROJECT_NAME in out

        code = cli_main(["--database", str(db_path), "inspect", TEST_PROJECT_NAME])
        out = capsys.readouterr().out
        assert code == 0
        assert "healthy-api" in out
        assert "redis" in out
        assert "postgres" in out

        code = cli_main(["--database", str(db_path), "incidents"])
        assert code == 0
        capsys.readouterr()

        code = cli_main(["--database", str(db_path), "incidents", "--open"])
        assert code == 0
        capsys.readouterr()

        code = cli_main(["--database", str(db_path), "history", TEST_PROJECT_NAME, "--since", "24h"])
        out = capsys.readouterr().out
        assert code == 0
        assert "->" in out  # at minimum, the first-ever NULL -> HEALTHY transitions


# ==========================================================================
# Scenario 8 -- CLI end-to-end (JSON), explicit data-leak checks
# ==========================================================================


class TestScenario8CliJsonEndToEnd:
    def test_json_output_never_leaks_labels_env_or_host_paths(self, stack, argus_db, capsys):
        db_path, connection, repository = argus_db
        client = DockerClient()
        loop = CollectorLoop(client=client, repository=repository, config=TEST_CONFIG, rules=TEST_RULES)
        assert loop.run_once().success

        code = cli_main(["--database", str(db_path), "inspect", TEST_PROJECT_NAME, "--json"])
        raw = capsys.readouterr().out
        assert code == 0
        payload = json.loads(raw)  # must be valid, parseable JSON
        assert payload["key"] == TEST_PROJECT_NAME

        for forbidden in ("labels", "Env", "com.docker.compose", "/Users", "/home", "POSTGRES_PASSWORD"):
            assert forbidden not in raw, f"{forbidden!r} leaked into inspect --json output"

        code = cli_main(["--database", str(db_path), "status", "--json"])
        raw_status = capsys.readouterr().out
        assert code == 0
        json.loads(raw_status)
        for forbidden in ("labels", "Env", "com.docker.compose"):
            assert forbidden not in raw_status, f"{forbidden!r} leaked into status --json output"


# ==========================================================================
# Scenario 9 -- doctor end-to-end (healthy, then stale)
# ==========================================================================


class TestScenario9DoctorEndToEnd:
    def test_doctor_reports_healthy_then_stale_after_collector_stops_ticking(
        self, stack, argus_db, capsys
    ):
        db_path, connection, repository = argus_db
        client = DockerClient()
        loop = CollectorLoop(client=client, repository=repository, config=TEST_CONFIG, rules=TEST_RULES)
        assert loop.run_once().success

        code = cli_main(["--database", str(db_path), "doctor"])
        out = capsys.readouterr().out
        assert code == 0
        assert "Argus is operational." in out

        payload = json.loads(
            _run_and_capture(["--database", str(db_path), "doctor", "--json"], capsys)
        )
        assert payload["operational"] is True
        heartbeat = next(c for c in payload["checks"] if c["name"] == "collector_heartbeat")
        assert heartbeat["status"] == "PASS"

        # `argus doctor` always judges collector-heartbeat staleness against
        # DEFAULT_HEALTH_RULES (argus/doctor/checks.py's own run_checks
        # default) -- there is no settings system yet for it to learn this
        # test's faster TEST_RULES from, by design (see Milestone 8's
        # report). So this waits past the *real* default unknown_after, not
        # this file's TEST_UNKNOWN_AFTER. Docker itself is never touched
        # from here on -- only the collector stops ticking.
        time.sleep(DEFAULT_HEALTH_RULES.unknown_after + 3)

        payload = json.loads(
            _run_and_capture(["--database", str(db_path), "doctor", "--json"], capsys)
        )
        assert payload["operational"] is False
        heartbeat = next(c for c in payload["checks"] if c["name"] == "collector_heartbeat")
        assert heartbeat["status"] == "FAIL"
        docker_connection = next(c for c in payload["checks"] if c["name"] == "docker_connection")
        assert docker_connection["status"] == "PASS"  # Docker itself was never touched

        code = cli_main(["--database", str(db_path), "doctor"])
        capsys.readouterr()
        assert code == 1


def _run_and_capture(argv: list[str], capsys) -> str:
    cli_main(argv)
    return capsys.readouterr().out


# ==========================================================================
# Milestone 6 hardening -- transition timestamp accuracy, end-to-end
# ==========================================================================


class TestTransitionTimestampAccuracyEndToEnd:
    def test_processing_failure_gap_backfills_the_true_occurred_at(
        self, stack, raw_docker, argus_db, monkeypatch
    ):
        db_path, connection, repository = argus_db
        client = DockerClient()

        t0 = datetime(2026, 8, 22, 9, 0, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(seconds=30)
        t2 = t1 + timedelta(seconds=30)
        clock_values = iter([t0, t1, t2])
        loop = CollectorLoop(
            client=client, repository=repository, config=TEST_CONFIG, rules=TEST_RULES,
            clock=lambda: next(clock_values),
        )

        container_id = compose_container_id("healthy-api")
        try:
            baseline = loop.run_once()
            assert baseline.success
            assert repository.get_open_incident(failure_signature=APPLICATION_FAILURE_SIGNATURE) is None

            safe_stop(raw_docker, container_id)
            wait_until(
                lambda: raw_docker.containers.get(container_id).status == "exited",
                timeout=20, interval=1, description="healthy-api exited before T1's tick",
            )

            def boom(**kwargs):
                raise IncidentProcessingError("simulated transition/incident processing failure at T1")

            monkeypatch.setattr(loop_module, "process_transitions_and_incidents", boom)
            t1_result = loop.run_once()
            assert t1_result.success is False

            # persist_discovery's own transaction already committed before
            # incident processing (which failed) ever ran -- the UNHEALTHY-
            # proving observation is already sitting in the database at T1.
            t1_observation = repository.get_latest_observation(container_id)
            assert t1_observation.observed_at == t1

            monkeypatch.undo()  # restore the real function for T2
            t2_result = loop.run_once()
            assert t2_result.success

            app_record = repository.get_application(TEST_PROJECT_NAME)
            transition = repository.get_last_transition(scope="application", scope_id=app_record.id)
            assert transition.to_status is HealthStatus.UNHEALTHY
            # backfilled to T1 (when the evidence genuinely first appeared),
            # not T2 (the tick that happened to finally record it).
            assert transition.occurred_at == t1

            open_incident = repository.get_open_incident(failure_signature=APPLICATION_FAILURE_SIGNATURE)
            assert open_incident is not None
        finally:
            safe_start(raw_docker, container_id)
            wait_until(
                lambda: _argus_test_stack_is_healthy(DockerClient()),
                timeout=30, interval=2, description="argus-test-stack healthy before next test",
                on_timeout=lambda: "leaving healthy-api in whatever state it is in -- see the failure above",
            )


# ==========================================================================
# Independent safety-guard tests
#
# These don't require the disposable stack at all -- they prove the guard
# itself refuses to mutate anything outside argus-test-stack, using real
# project names already present in this very repository's own Docker
# fixtures (tests/fixtures/docker_responses/), plus the real, currently
# running containers on this host.
# ==========================================================================


class TestSafetyGuardRejectsOtherRealProjects:
    @pytest.mark.parametrize("project_label", ["cnstrct", "musipal", "sample-project"])
    def test_refuses_other_real_compose_projects(self, project_label):
        attrs = {"Config": {"Labels": {"com.docker.compose.project": project_label}}}
        with pytest.raises(UnsafeMutationError):
            assert_safe_to_mutate(attrs)

    def test_refuses_standalone_container_with_no_compose_labels(self):
        attrs = {"Config": {"Labels": {}}}
        with pytest.raises(UnsafeMutationError):
            assert_safe_to_mutate(attrs)

    def test_refuses_container_with_only_the_service_label_and_no_project_label(self):
        # An incomplete/unexpected label shape -- never treated as "close
        # enough" to argus-test-stack.
        attrs = {"Config": {"Labels": {"com.docker.compose.service": "api"}}}
        with pytest.raises(UnsafeMutationError):
            assert_safe_to_mutate(attrs)

    def test_rejects_prefix_and_substring_lookalikes_no_fuzzy_matching(self):
        lookalikes = ["argus-test-stack-old", "ARGUS-TEST-STACK", "argus-test-stac", "argus-test-stack2"]
        for lookalike in lookalikes:
            attrs = {"Config": {"Labels": {"com.docker.compose.project": lookalike}}}
            with pytest.raises(UnsafeMutationError):
                assert_safe_to_mutate(attrs)

    def test_accepts_only_the_exact_project_name(self):
        attrs = {"Config": {"Labels": {"com.docker.compose.project": TEST_PROJECT_NAME}}}
        assert_safe_to_mutate(attrs)  # does not raise


class TestSafetyGuardRejectsRealHostContainers:
    def test_refuses_to_stop_a_real_non_test_container_on_this_host(self, raw_docker):
        non_test = snapshot_non_test_containers(raw_docker)
        if not non_test:
            pytest.skip("no non-test containers present on this host to prove the guard against")

        container_id, status_before = next(iter(non_test.items()))
        with pytest.raises(UnsafeMutationError):
            safe_stop(raw_docker, container_id)

        # The guard must raise *before* any Docker call is made -- the
        # container's status is provably unchanged.
        assert raw_docker.containers.get(container_id).status == status_before
