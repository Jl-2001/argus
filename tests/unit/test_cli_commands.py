"""End-to-end CLI tests: invoke argus.cli.main.main() with argv, assert
on captured stdout/stderr and exit codes. Populates temporary databases
directly through Repository -- no Docker, no collector process running.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import inspect
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from argus.cli import durations, formatting, main as main_module, queries
from argus.cli.commands import apps, bundle, evidence, history, incidents, inspect as inspect_cmd, status
from argus.domain.models import HealthStatus
from argus.store.database import open_database
from argus.store.repository import Repository

UTC = timezone.utc
NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)

# Tests that go through `run_cli` (i.e. `main.main()`) exercise the real
# `datetime.now(timezone.utc)` read at the command boundary -- data seeded
# for those must be relative to the *real* clock, not the fixed `NOW`
# above. `NOW` itself is only safe for tests that call a command's `run()`
# directly with an injected `now` (the snapshot tests below).
#
# `real_now()` is a function, not a module-level constant, deliberately:
# a constant captured once at collection time can silently go stale by
# the time a given test actually executes -- and a full-suite run
# (especially alongside Milestone 9's real, wall-clock-bound Docker
# integration tests, which now often take over a minute on their own)
# can easily exceed DEFAULT_HEALTH_RULES.unknown_after (60s) between
# collection and this file's own tests running. A function call here
# means every seed always reflects the real "now" at the moment it's
# actually used, however long the rest of the suite took to get there.
def real_now() -> datetime:
    return datetime.now(UTC)


def seed_full_stack(repository, *, key, name, compose_service, container_id, at, status):
    repository.upsert_application(key=key, name=name, is_standalone=False, observed_at=at)
    app = repository.get_application(key)
    repository.upsert_service(application_id=app.id, compose_service=compose_service, name=compose_service, observed_at=at)
    service = repository.get_service_by_key(application_id=app.id, compose_service=compose_service)
    repository.upsert_container(
        service_id=service.id, container_id=container_id, name=f"{key}-{compose_service}-1",
        first_seen_at=at, last_seen_at=at,
    )
    container_record = repository.get_container_by_docker_id(container_id)
    for scope, scope_id in (("container", container_record.id), ("service", service.id), ("application", app.id)):
        repository.insert_transition(scope=scope, scope_id=scope_id, from_status=None, to_status=status, occurred_at=at)
    return app, service, container_record


def run_cli(db_path: Path, *args: str) -> int:
    return main_module.main(["--database", str(db_path), *args])


class TestHelp:
    def test_top_level_help_lists_commands(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main_module.main(["--help"])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        for command in ("status", "apps", "inspect", "incidents", "history", "evidence", "bundle"):
            assert command in out


class TestStatus:
    def test_never_run_empty_database(self, tmp_path, capsys):
        db_path = tmp_path / "a.db"
        code = run_cli(db_path, "status")
        out = capsys.readouterr().out
        assert code == 0
        assert "NEVER_RUN" in out
        assert "No applications discovered yet." in out

    def test_populated_healthy_database(self, tmp_path, capsys):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        seed_full_stack(repo, key="cnstrct", name="CNSTRCT", compose_service="api", container_id="aaa", at=real_now(), status=HealthStatus.HEALTHY)
        repo.record_tick_started(at=real_now())
        repo.record_tick_success(at=real_now())
        conn.close()

        code = run_cli(db_path, "status")
        out = capsys.readouterr().out
        assert code == 0
        assert "HEALTHY" in out
        assert "CNSTRCT" in out
        assert "1 application" in out
        assert "0 open incidents" in out

    def test_failing_collector_shows_last_error(self, tmp_path, capsys):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        repo.record_tick_started(at=real_now())
        repo.record_tick_failure(error="DockerUnavailableError: could not connect")
        conn.close()

        code = run_cli(db_path, "status")
        out = capsys.readouterr().out
        assert code == 0
        assert "FAILING" in out
        assert "DockerUnavailableError: could not connect" in out

    def test_json_is_parseable(self, tmp_path, capsys):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        seed_full_stack(repo, key="cnstrct", name="CNSTRCT", compose_service="api", container_id="aaa", at=real_now(), status=HealthStatus.HEALTHY)
        repo.record_tick_started(at=real_now())
        repo.record_tick_success(at=real_now())
        conn.close()

        run_cli(db_path, "status", "--json")
        payload = json.loads(capsys.readouterr().out)
        assert payload["applications"][0]["name"] == "CNSTRCT"
        assert payload["collector"]["status"] in ("HEALTHY", "STALE", "FAILING", "NEVER_RUN")


class TestApps:
    def test_empty(self, tmp_path, capsys):
        code = run_cli(tmp_path / "a.db", "apps")
        assert code == 0
        assert "No applications discovered yet." in capsys.readouterr().out

    def test_deterministic_ordering_and_json(self, tmp_path, capsys):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        seed_full_stack(repo, key="zzz-app", name="ZApp", compose_service="svc", container_id="z1", at=real_now(), status=HealthStatus.HEALTHY)
        seed_full_stack(repo, key="aaa-app", name="AApp", compose_service="svc", container_id="a1", at=real_now(), status=HealthStatus.HEALTHY)
        repo.record_tick_started(at=real_now())
        repo.record_tick_success(at=real_now())
        conn.close()

        run_cli(db_path, "apps")
        out = capsys.readouterr().out
        assert out.index("AApp") < out.index("ZApp")  # key-ascending: aaa-app before zzz-app

        run_cli(db_path, "apps", "--json")
        payload = json.loads(capsys.readouterr().out)
        assert [a["key"] for a in payload["applications"]] == ["aaa-app", "zzz-app"]


class TestInspect:
    def test_found_application(self, tmp_path, capsys):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        seed_full_stack(repo, key="cnstrct", name="CNSTRCT", compose_service="api", container_id="aaa", at=real_now(), status=HealthStatus.HEALTHY)
        repo.record_tick_started(at=real_now())
        repo.record_tick_success(at=real_now())
        conn.close()

        code = run_cli(db_path, "inspect", "CNSTRCT")
        out = capsys.readouterr().out
        assert code == 0
        assert "CNSTRCT · HEALTHY" in out
        assert "api" in out

    def test_not_found_with_suggestion_exits_1(self, tmp_path, capsys):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        seed_full_stack(repo, key="cnstrct", name="CNSTRCT", compose_service="api", container_id="aaa", at=real_now(), status=HealthStatus.HEALTHY)
        conn.close()

        code = run_cli(db_path, "inspect", "cnstrt")
        captured = capsys.readouterr()
        assert code == 1
        assert "not found" in captured.err
        assert "Did you mean 'CNSTRCT'?" in captured.err

    def test_no_sensitive_data_in_output(self, tmp_path, capsys):
        """Labels are already allowlist-filtered by Milestone 3 before they
        are ever persisted, and queries.py never even reads .labels --
        this asserts the CLI surface reflects that, not just trusts it."""
        from argus.domain.models import Container, DockerHealth, DockerState, Observation

        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        app, service, container_record = seed_full_stack(
            repo, key="cnstrct", name="CNSTRCT", compose_service="api", container_id="aaa", at=real_now(),
            status=HealthStatus.HEALTHY,
        )
        container = Container(
            container_id="aaa", name="cnstrct-api-1", image="cnstrct/api:1", compose_project="cnstrct",
            compose_service="api", first_seen_at=real_now(), last_seen_at=real_now(),
        )
        observation = Observation(
            container_ref=container, observed_at=real_now(), docker_state=DockerState.RUNNING,
            docker_health=DockerHealth.HEALTHY, restart_count=0, exit_code=None, started_at=None,
            finished_at=None, ports=(),
            labels={"com.docker.compose.project": "cnstrct", "argus.owner": "jorge"},
            derived_status=HealthStatus.HEALTHY, derived_detail=None,
        )
        repo.insert_observation(container_row_id=container_record.id, observation=observation)
        repo.record_tick_started(at=real_now())
        repo.record_tick_success(at=real_now())
        conn.close()

        run_cli(db_path, "inspect", "CNSTRCT", "--json")
        payload = json.loads(capsys.readouterr().out)
        assert "labels" not in json.dumps(payload)
        assert "jorge" not in json.dumps(payload)


class TestIncidents:
    def test_open_filter_and_ordering(self, tmp_path, capsys):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)

        app1, *_ = seed_full_stack(repo, key="alpha-app", name="AlphaApp", compose_service="svc", container_id="c1", at=real_now() - timedelta(hours=3), status=HealthStatus.UNHEALTHY)
        t1 = repo.get_last_transition(scope="application", scope_id=app1.id)
        inc1 = repo.open_incident(scope_id=app1.id, failure_signature="application:alpha-app", opened_at=real_now() - timedelta(hours=3), opening_status=HealthStatus.UNHEALTHY, opening_transition_id=t1.id)
        t1b = repo.insert_transition(scope="application", scope_id=app1.id, from_status=HealthStatus.UNHEALTHY, to_status=HealthStatus.HEALTHY, occurred_at=real_now() - timedelta(hours=2, minutes=57))
        repo.resolve_incident(incident_id=inc1, closed_at=real_now() - timedelta(hours=2, minutes=57), resolving_transition_id=t1b)

        app2, *_ = seed_full_stack(repo, key="beta-app", name="BetaApp", compose_service="svc", container_id="c2", at=real_now() - timedelta(minutes=2), status=HealthStatus.DEGRADED)
        t2 = repo.get_last_transition(scope="application", scope_id=app2.id)
        repo.open_incident(scope_id=app2.id, failure_signature="application:beta-app", opened_at=real_now() - timedelta(minutes=2), opening_status=HealthStatus.DEGRADED, opening_transition_id=t2.id)
        conn.close()

        code = run_cli(db_path, "incidents")
        out = capsys.readouterr().out
        assert code == 0
        assert out.index("BetaApp") < out.index("AlphaApp")  # newest opened_at first

        run_cli(db_path, "incidents", "--open")
        out2 = capsys.readouterr().out
        assert "AlphaApp" not in out2  # resolved incident filtered out entirely
        assert "resolved" not in out2

    def test_empty_message(self, tmp_path, capsys):
        code = run_cli(tmp_path / "a.db", "incidents")
        assert code == 0
        assert "No incidents recorded." in capsys.readouterr().out


class TestHistory:
    def test_since_window_and_label_resolution(self, tmp_path, capsys):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        seed_full_stack(repo, key="cnstrct", name="CNSTRCT", compose_service="api", container_id="aaa", at=real_now() - timedelta(minutes=10), status=HealthStatus.HEALTHY)
        conn.close()

        code = run_cli(db_path, "history", "CNSTRCT", "--since", "30m")
        out = capsys.readouterr().out
        assert code == 0
        assert "application" in out
        assert "api" in out
        assert "NULL -> HEALTHY" in out

    def test_since_window_excludes_older_transitions(self, tmp_path, capsys):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        seed_full_stack(repo, key="cnstrct", name="CNSTRCT", compose_service="api", container_id="aaa", at=real_now() - timedelta(hours=2), status=HealthStatus.HEALTHY)
        conn.close()

        code = run_cli(db_path, "history", "CNSTRCT", "--since", "30m")
        out = capsys.readouterr().out
        assert code == 0
        assert "No transitions recorded in this window." in out

    def test_invalid_since_is_argument_error_exit_2(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            run_cli(tmp_path / "a.db", "history", "CNSTRCT", "--since", "yesterday-ish")
        assert exc_info.value.code == 2
        assert "traceback" not in capsys.readouterr().err.lower()

    def test_unknown_application(self, tmp_path, capsys):
        code = run_cli(tmp_path / "a.db", "history", "ghost")
        captured = capsys.readouterr()
        assert code == 1
        assert "not found" in captured.err


class TestDatabaseFailure:
    def test_path_is_a_directory_exits_1_without_traceback(self, tmp_path, capsys):
        code = run_cli(tmp_path, "status")  # tmp_path itself is a directory, not a file
        captured = capsys.readouterr()
        assert code == 1
        assert "Argus database unavailable" in captured.err
        assert "Traceback" not in captured.err


# --------------------------------------------------------------------------
# Snapshot-style exact output tests
# --------------------------------------------------------------------------


class TestSnapshots:
    def _populated_repo(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        app, *_ = seed_full_stack(
            repo, key="cnstrct", name="CNSTRCT", compose_service="api", container_id="aaa",
            at=NOW - timedelta(minutes=2), status=HealthStatus.DEGRADED,
        )
        transition = repo.get_last_transition(scope="application", scope_id=app.id)
        repo.open_incident(
            scope_id=app.id, failure_signature="application:cnstrct",
            opened_at=NOW - timedelta(minutes=2), opening_status=HealthStatus.DEGRADED,
            opening_transition_id=transition.id,
        )
        repo.record_tick_started(at=NOW - timedelta(seconds=3))
        repo.record_tick_success(at=NOW - timedelta(seconds=3))
        return conn, repo

    def test_status_snapshot(self, tmp_path):
        conn, repo = self._populated_repo(tmp_path)
        ns = _namespace(json=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            status.run(ns, repo, NOW)
        conn.close()
        assert buf.getvalue() == (
            "ARGUS\n\n"
            "Collector\n"
            "Status        HEALTHY\n"
            "Last tick     3s ago\n"
            "Last success  3s ago\n"
            "Failures      0\n\n"
            "Applications\n\n"
            "NAME     STATUS    SERVICES  CONTAINERS\n"
            "CNSTRCT  DEGRADED  1         1\n\n"
            "1 application · 1 open incident\n"
        )

    def test_inspect_snapshot(self, tmp_path):
        conn, repo = self._populated_repo(tmp_path)
        ns = _namespace(application="CNSTRCT", json=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            inspect_cmd.run(ns, repo, NOW)
        conn.close()
        assert buf.getvalue() == (
            "CNSTRCT · DEGRADED\n\n"
            "SERVICE  STATUS    CONTAINER  DOCKER STATE  DOCKER HEALTH  RESTARTS\n"
            "api      DEGRADED  —          —             —              —\n\n"
            "Open incident #1\n"
            "Opened        2m ago\n"
            "Opening state DEGRADED\n"
            "Worst state   DEGRADED\n"
        )

    def test_incidents_open_snapshot(self, tmp_path):
        conn, repo = self._populated_repo(tmp_path)
        ns = _namespace(open_only=True, json=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            incidents.run(ns, repo, NOW)
        conn.close()
        assert buf.getvalue() == (
            "ID  APPLICATION  STATUS  OPENED  CLOSED  WORST\n"
            "1   CNSTRCT      open    2m ago  —       DEGRADED\n"
        )


def _namespace(**kwargs):
    return argparse.Namespace(**kwargs)


# --------------------------------------------------------------------------
# Architecture guard
# --------------------------------------------------------------------------

FORBIDDEN_IMPORT_ROOTS = {"docker", "anthropic", "openai", "langgraph", "fastapi", "requests", "httpx"}

_CLI_MODULES = (
    main_module, queries, formatting, durations, status, apps, inspect_cmd, incidents, history, evidence, bundle,
)


def _imported_roots(source: str) -> set[str]:
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _imported_dotted_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _called_function_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


class TestArchitectureGuard:
    def test_no_forbidden_imports_anywhere_in_cli(self):
        for module in _CLI_MODULES:
            source = inspect.getsource(module)
            found = _imported_roots(source) & FORBIDDEN_IMPORT_ROOTS
            assert not found, f"{module.__name__} imports forbidden module(s): {found}"

    def test_cli_never_imports_collectors_or_incidents_engine(self):
        for module in _CLI_MODULES:
            source = inspect.getsource(module)
            modules = _imported_dotted_modules(source)
            assert not any(m.startswith("argus.collectors") for m in modules), module.__name__
            assert not any(m.startswith("argus.incidents") for m in modules), module.__name__

    def test_cli_never_calls_health_evaluators(self):
        forbidden = {"evaluate_container_health", "evaluate_service_health", "evaluate_application_health"}
        for module in _CLI_MODULES:
            called = _called_function_names(inspect.getsource(module))
            assert not (called & forbidden), f"{module.__name__} calls: {called & forbidden}"

    def test_cli_never_calls_write_operations(self):
        forbidden = {
            "insert_transition", "insert_observation", "upsert_application", "upsert_service",
            "upsert_container", "persist_discovery", "record_tick_started", "record_tick_success",
            "record_tick_failure", "open_incident", "resolve_incident", "update_incident_worst_status",
            "process_transitions_and_incidents",
        }
        for module in _CLI_MODULES:
            called = _called_function_names(inspect.getsource(module))
            assert not (called & forbidden), f"{module.__name__} calls: {called & forbidden}"
