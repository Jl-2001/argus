"""Tests for argus.doctor.checks -- the six live prerequisite checks
behind `argus doctor`. No live Docker daemon required: Docker behavior
is injected via `docker_client_factory`. Temporary, file-backed SQLite
databases only -- no shared/production state.
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from argus.collectors.docker_client import ContainerAttrs, DockerClient, DockerUnavailableError
from argus.doctor import checks as checks_module
from argus.doctor.checks import (
    CLOCK_TOLERANCE_SECONDS,
    CheckStatus,
    DoctorResult,
    check_clock,
    check_collector_heartbeat,
    check_configuration,
    check_database,
    check_docker_connection,
    check_docker_read_access,
    run_checks,
)
from argus.domain.health import DEFAULT_HEALTH_RULES
from argus.store.database import open_database
from argus.store.repository import Repository

UTC = timezone.utc
NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)
UNKNOWN_AFTER = DEFAULT_HEALTH_RULES.unknown_after


# --------------------------------------------------------------------------
# Fake Docker plumbing -- injected via docker_client_factory, no real daemon
# --------------------------------------------------------------------------


class _FakeContainer:
    def __init__(self, id: str, attrs: dict):
        self.id = id
        self.attrs = attrs


class _FakeContainersAPI:
    def __init__(self, containers=None, list_error=None):
        self._containers = containers or []
        self._list_error = list_error

    def list(self, all=False):
        if self._list_error is not None:
            raise self._list_error
        return [_FakeContainer(c, {}) for c in self._containers]

    def get(self, container_id):
        return _FakeContainer(container_id, {})


class _FakeSDKClient:
    def __init__(self, containers=None, list_error=None):
        self.containers = _FakeContainersAPI(containers, list_error)


def working_docker_factory():
    return DockerClient(client=_FakeSDKClient(containers=["a", "b"]))


def unreachable_docker_factory():
    import docker.errors

    raise DockerUnavailableError("could not connect to Docker: no such file or directory")


def permission_denied_docker_factory():
    import docker.errors

    return DockerClient(
        client=_FakeSDKClient(list_error=docker.errors.APIError("permission denied"))
    )


def result_by_name(result: DoctorResult, name: str):
    return next(check for check in result.checks if check.name == name)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


class TestConfigurationCheck:
    def test_passes_with_normal_defaults(self, tmp_path):
        result = check_configuration(tmp_path / "a.db")
        assert result.status is CheckStatus.PASS

    def test_fails_on_empty_docker_host(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOCKER_HOST", "   ")
        result = check_configuration(tmp_path / "a.db")
        assert result.status is CheckStatus.FAIL
        assert "DOCKER_HOST" in result.message


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------


class TestDatabaseCheck:
    def test_missing_database_fails_and_creates_nothing(self, tmp_path):
        db_path = tmp_path / "does-not-exist.db"
        result = check_database(db_path)
        assert result.status is CheckStatus.FAIL
        assert "does not exist" in result.message
        assert not db_path.exists()

    def test_malformed_file_fails_clearly(self, tmp_path):
        db_path = tmp_path / "a.db"
        db_path.write_text("this is not a sqlite database")
        result = check_database(db_path)
        assert result.status is CheckStatus.FAIL
        assert "malformed" in result.message

    def test_valid_database_passes(self, tmp_path):
        db_path = tmp_path / "a.db"
        open_database(db_path).close()
        result = check_database(db_path)
        assert result.status is CheckStatus.PASS

    def test_older_schema_version_fails_without_migrating(self, tmp_path):
        """Doctor never repairs -- even though open_database() would happily
        migrate a v1/v2 database forward, doctor's read-only inspection
        must not trigger that, and must report the mismatch instead."""

        db_path = tmp_path / "a.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            "CREATE TABLE applications (id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, "
            "name TEXT NOT NULL, is_standalone INTEGER NOT NULL, first_seen_at TEXT NOT NULL, "
            "last_seen_at TEXT NOT NULL);"
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

        result = check_database(db_path)
        assert result.status is CheckStatus.FAIL
        assert "schema version" in result.message
        # confirm doctor did not migrate it in place
        conn2 = sqlite3.connect(str(db_path))
        assert conn2.execute("PRAGMA user_version").fetchone()[0] == 1
        conn2.close()

    def test_future_schema_version_fails(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        conn.execute("PRAGMA user_version = 99")
        conn.close()

        result = check_database(db_path)
        assert result.status is CheckStatus.FAIL
        assert "schema version" in result.message


# --------------------------------------------------------------------------
# Docker connection / read access
# --------------------------------------------------------------------------


class TestDockerChecks:
    def test_connection_and_read_access_pass_together(self):
        connection_check, client = check_docker_connection(working_docker_factory)
        assert connection_check.status is CheckStatus.PASS
        read_check = check_docker_read_access(client)
        assert read_check.status is CheckStatus.PASS

    def test_unreachable_daemon_skips_read_access(self):
        connection_check, client = check_docker_connection(unreachable_docker_factory)
        assert connection_check.status is CheckStatus.FAIL
        assert client is None
        read_check = check_docker_read_access(client)
        assert read_check.status is CheckStatus.SKIP
        assert "connection failed" in read_check.message

    def test_reachable_but_permission_denied_fails_read_access_only(self):
        connection_check, client = check_docker_connection(permission_denied_docker_factory)
        assert connection_check.status is CheckStatus.PASS
        read_check = check_docker_read_access(client)
        assert read_check.status is CheckStatus.FAIL


# --------------------------------------------------------------------------
# Collector heartbeat
# --------------------------------------------------------------------------


class TestCollectorHeartbeatCheck:
    def test_skipped_when_database_unavailable(self, tmp_path):
        result = check_collector_heartbeat(tmp_path / "a.db", database_ok=False, now=NOW)
        assert result.status is CheckStatus.SKIP

    def test_never_run_fails(self, tmp_path):
        db_path = tmp_path / "a.db"
        open_database(db_path).close()
        result = check_collector_heartbeat(db_path, database_ok=True, now=NOW)
        assert result.status is CheckStatus.FAIL
        assert "never completed a tick" in result.message

    def test_ticked_but_never_succeeded_fails(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        Repository(conn).record_tick_started(at=NOW)
        conn.close()
        result = check_collector_heartbeat(db_path, database_ok=True, now=NOW)
        assert result.status is CheckStatus.FAIL
        assert "never succeeded" in result.message

    def test_healthy_passes(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        repo.record_tick_started(at=NOW - timedelta(seconds=5))
        repo.record_tick_success(at=NOW - timedelta(seconds=5))
        conn.close()
        result = check_collector_heartbeat(db_path, database_ok=True, now=NOW)
        assert result.status is CheckStatus.PASS

    def test_failing_but_within_freshness_window_warns(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        repo.record_tick_started(at=NOW - timedelta(seconds=30))
        repo.record_tick_success(at=NOW - timedelta(seconds=30))
        repo.record_tick_started(at=NOW - timedelta(seconds=5))
        repo.record_tick_failure(error="transient")
        conn.close()
        result = check_collector_heartbeat(db_path, database_ok=True, now=NOW)
        assert result.status is CheckStatus.WARN
        assert "within the freshness window" in result.message

    def test_stale_beyond_unknown_after_fails(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        old = NOW - timedelta(seconds=UNKNOWN_AFTER + 1)
        repo.record_tick_started(at=old)
        repo.record_tick_success(at=old)
        conn.close()
        result = check_collector_heartbeat(db_path, database_ok=True, now=NOW)
        assert result.status is CheckStatus.FAIL
        assert "stale" in result.message

    def test_exactly_at_unknown_after_boundary_does_not_fail(self, tmp_path):
        """Matches the strictly-greater-than convention used everywhere
        else in Argus (see argus.domain.health)."""

        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        boundary = NOW - timedelta(seconds=UNKNOWN_AFTER)
        repo.record_tick_started(at=boundary)
        repo.record_tick_success(at=boundary)
        conn.close()
        result = check_collector_heartbeat(db_path, database_ok=True, now=NOW)
        assert result.status is CheckStatus.PASS


# --------------------------------------------------------------------------
# Clock
# --------------------------------------------------------------------------


class TestClockCheck:
    def test_naive_now_fails(self, tmp_path):
        naive_now = datetime(2026, 8, 22, 12, 0, 0)  # no tzinfo
        result = check_clock(tmp_path / "a.db", database_ok=False, now=naive_now)
        assert result.status is CheckStatus.FAIL
        assert "UTC-aware" in result.message

    def test_passes_without_database(self, tmp_path):
        result = check_clock(tmp_path / "a.db", database_ok=False, now=NOW)
        assert result.status is CheckStatus.PASS

    def test_future_last_tick_at_fails(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        future = NOW + timedelta(minutes=10)
        repo.record_tick_started(at=future)
        repo.record_tick_success(at=future)
        conn.close()
        result = check_clock(db_path, database_ok=True, now=NOW)
        assert result.status is CheckStatus.FAIL
        assert "ahead of the system clock" in result.message

    def test_within_tolerance_passes(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        slightly_ahead = NOW + timedelta(seconds=CLOCK_TOLERANCE_SECONDS - 1)
        repo.record_tick_started(at=slightly_ahead)
        conn.close()
        result = check_clock(db_path, database_ok=True, now=NOW)
        assert result.status is CheckStatus.PASS

    def test_last_success_after_last_tick_beyond_tolerance_fails_clock_not_heartbeat(self, tmp_path):
        """Ownership: this impossible ordering is Clock's to report, not
        Collector heartbeat's -- ordering defect vs. freshness judgment."""

        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        tick_at = NOW - timedelta(seconds=30)
        success_at = tick_at + timedelta(seconds=CLOCK_TOLERANCE_SECONDS + 5)  # "succeeded" after the tick started, beyond tolerance
        repo.record_tick_started(at=tick_at)
        repo.record_tick_success(at=success_at)
        conn.close()

        clock_result = check_clock(db_path, database_ok=True, now=NOW)
        heartbeat_result = check_collector_heartbeat(db_path, database_ok=True, now=NOW)

        assert clock_result.status is CheckStatus.FAIL
        assert "impossible ordering" in clock_result.message
        # heartbeat still judges freshness on its own terms, independent of the ordering defect
        assert heartbeat_result.status is CheckStatus.PASS


# --------------------------------------------------------------------------
# Orchestration: run_checks / DoctorResult
# --------------------------------------------------------------------------


class TestRunChecks:
    def test_all_healthy(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        repo.record_tick_started(at=NOW - timedelta(seconds=5))
        repo.record_tick_success(at=NOW - timedelta(seconds=5))
        conn.close()

        result = run_checks(db_path=db_path, now=NOW, docker_client_factory=working_docker_factory)
        assert all(c.status is CheckStatus.PASS for c in result.checks)
        assert result.operational is True

    def test_docker_unavailable_end_to_end(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        repo.record_tick_started(at=NOW - timedelta(seconds=5))
        repo.record_tick_success(at=NOW - timedelta(seconds=5))
        conn.close()

        result = run_checks(db_path=db_path, now=NOW, docker_client_factory=unreachable_docker_factory)
        assert result_by_name(result, "docker_connection").status is CheckStatus.FAIL
        assert result_by_name(result, "docker_read_access").status is CheckStatus.SKIP
        assert result.operational is False

    def test_database_missing_end_to_end_creates_nothing(self, tmp_path):
        db_path = tmp_path / "does-not-exist.db"
        result = run_checks(db_path=db_path, now=NOW, docker_client_factory=working_docker_factory)
        assert result_by_name(result, "database").status is CheckStatus.FAIL
        assert result_by_name(result, "collector_heartbeat").status is CheckStatus.SKIP
        assert result.operational is False
        assert not db_path.exists()

    def test_warn_alone_is_still_operational(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        repo.record_tick_started(at=NOW - timedelta(seconds=30))
        repo.record_tick_success(at=NOW - timedelta(seconds=30))
        repo.record_tick_started(at=NOW - timedelta(seconds=5))
        repo.record_tick_failure(error="transient")
        conn.close()

        result = run_checks(db_path=db_path, now=NOW, docker_client_factory=working_docker_factory)
        assert result_by_name(result, "collector_heartbeat").status is CheckStatus.WARN
        assert result.operational is True  # WARN alone does not make Argus non-operational

    def test_check_order_is_fixed_regardless_of_outcome(self, tmp_path):
        db_path = tmp_path / "does-not-exist.db"
        result = run_checks(db_path=db_path, now=NOW, docker_client_factory=unreachable_docker_factory)
        assert [c.name for c in result.checks] == [
            "configuration",
            "database",
            "docker_connection",
            "docker_read_access",
            "collector_heartbeat",
            "clock",
        ]


# --------------------------------------------------------------------------
# Architecture / read-only guards
# --------------------------------------------------------------------------

FORBIDDEN_IMPORT_ROOTS = {"anthropic", "openai", "langgraph", "fastapi", "requests", "httpx"}

_MUTATING_CALL_PATTERNS = (
    ".start(", ".stop(", ".restart(", ".kill(", ".remove(", ".exec_run(",
    ".pause(", ".unpause(", ".rename(", ".update(", ".prune(", ".build(",
    ".pull(", ".push(", ".create(", ".run(", ".commit(",
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


class TestArchitectureGuard:
    def test_no_forbidden_imports(self):
        source = inspect.getsource(checks_module)
        found = _imported_roots(source) & FORBIDDEN_IMPORT_ROOTS
        assert not found, f"argus.doctor.checks imports forbidden module(s): {found}"

    def test_may_import_docker_client_and_store(self):
        source = inspect.getsource(checks_module)
        roots = _imported_roots(source)
        assert "argus" in roots

    def test_no_mutating_docker_calls(self):
        source = inspect.getsource(checks_module)
        found = [p for p in _MUTATING_CALL_PATTERNS if p in source]
        assert not found, f"argus.doctor.checks contains mutating Docker call(s): {found}"
