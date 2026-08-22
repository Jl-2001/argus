"""Tests for the `argus doctor` CLI surface: argparse wiring, main.py's
special dispatch (doctor must run even when the database can't be
opened), human/JSON formatting, and the read-only guard at the CLI
layer. Docker is mocked at the SDK entrypoint (`docker.from_env`) for
the end-to-end tests that go through `main.main()`, since that path
uses `run_checks`'s baked-in default factory rather than an injectable
one -- see `argus.doctor.checks.run_checks`'s own tests for the
factory-injection-based unit tests.
"""

from __future__ import annotations

import ast
import inspect
import json

import docker.errors
import pytest

from argus.cli import main as main_module
from argus.cli.commands import doctor as doctor_module
from argus.collectors import docker_client as docker_client_module
from argus.doctor.checks import CheckStatus, DoctorCheck, DoctorResult


class _FakeContainersAPI:
    def list(self, all=False):
        return []


class _FakeSDKClient:
    def __init__(self, **kwargs):
        self.containers = _FakeContainersAPI()


def patch_docker_reachable(monkeypatch):
    monkeypatch.setattr(docker_client_module.docker, "from_env", lambda **kw: _FakeSDKClient())


def patch_docker_unreachable(monkeypatch):
    def boom(**kwargs):
        raise docker.errors.DockerException("no daemon")

    monkeypatch.setattr(docker_client_module.docker, "from_env", boom)


def run_cli(db_path, *args) -> int:
    return main_module.main(["--database", str(db_path), *args])


class TestMainDispatch:
    def test_doctor_runs_even_when_database_is_missing(self, tmp_path, capsys, monkeypatch):
        patch_docker_reachable(monkeypatch)
        db_path = tmp_path / "does-not-exist.db"

        code = run_cli(db_path, "doctor")
        out = capsys.readouterr().out

        assert code == 1
        assert "ARGUS DOCTOR" in out  # doctor's own report, not the generic DB-unavailable message
        assert "Argus database unavailable" not in out
        assert not db_path.exists()  # doctor must never create it

    def test_doctor_json_via_main(self, tmp_path, capsys, monkeypatch):
        patch_docker_reachable(monkeypatch)
        db_path = tmp_path / "does-not-exist.db"

        run_cli(db_path, "doctor", "--json")
        payload = json.loads(capsys.readouterr().out)
        assert payload["operational"] is False
        assert any(check["name"] == "database" for check in payload["checks"])

    def test_doctor_all_healthy_via_main_exits_zero(self, tmp_path, capsys, monkeypatch):
        from datetime import datetime, timedelta, timezone

        from argus.store.database import open_database
        from argus.store.repository import Repository

        patch_docker_reachable(monkeypatch)
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        now = datetime.now(timezone.utc)
        repo = Repository(conn)
        repo.record_tick_started(at=now - timedelta(seconds=1))
        repo.record_tick_success(at=now - timedelta(seconds=1))
        conn.close()

        code = run_cli(db_path, "doctor")
        assert code == 0

    def test_other_commands_unaffected(self, tmp_path, capsys):
        """Milestone 8 must not change any existing command's behavior:
        a missing database still bootstraps normally for ordinary reads."""

        db_path = tmp_path / "a.db"
        code = run_cli(db_path, "status")
        out = capsys.readouterr().out
        assert code == 0
        assert "No applications discovered yet." in out
        assert db_path.exists()  # status's own open_database() legitimately bootstraps it

    def test_doctor_listed_in_top_level_help(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main_module.main(["--help"])
        assert exc_info.value.code == 0
        assert "doctor" in capsys.readouterr().out


class TestDoctorHelp:
    def test_doctor_help_lists_json_flag(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main_module.main(["doctor", "--help"])
        assert exc_info.value.code == 0
        assert "--json" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Human / JSON formatting -- exact snapshots against a canned DoctorResult
# --------------------------------------------------------------------------


def _namespace(json_flag: bool):
    import argparse

    return argparse.Namespace(json=json_flag)


ALL_PASS_RESULT = DoctorResult(
    checks=(
        DoctorCheck("configuration", CheckStatus.PASS, None),
        DoctorCheck("database", CheckStatus.PASS, None),
        DoctorCheck("docker_connection", CheckStatus.PASS, None),
        DoctorCheck("docker_read_access", CheckStatus.PASS, None),
        DoctorCheck("collector_heartbeat", CheckStatus.PASS, None),
        DoctorCheck("clock", CheckStatus.PASS, None),
    )
)

MIXED_RESULT = DoctorResult(
    checks=(
        DoctorCheck("configuration", CheckStatus.PASS, None),
        DoctorCheck("database", CheckStatus.PASS, None),
        DoctorCheck("docker_connection", CheckStatus.FAIL, "Cannot reach the configured Docker daemon."),
        DoctorCheck("docker_read_access", CheckStatus.SKIP, "skipped: Docker connection failed"),
        DoctorCheck(
            "collector_heartbeat", CheckStatus.WARN,
            "collector is currently failing (2 consecutive failures), but last success "
            "(30s ago) is still within the freshness window",
        ),
        DoctorCheck("clock", CheckStatus.PASS, None),
    )
)

WARN_ONLY_RESULT = DoctorResult(
    checks=(
        DoctorCheck("configuration", CheckStatus.PASS, None),
        DoctorCheck("database", CheckStatus.PASS, None),
        DoctorCheck("docker_connection", CheckStatus.PASS, None),
        DoctorCheck("docker_read_access", CheckStatus.PASS, None),
        DoctorCheck("collector_heartbeat", CheckStatus.WARN, "degraded but fresh"),
        DoctorCheck("clock", CheckStatus.PASS, None),
    )
)


class TestHumanFormatting:
    def test_all_pass_snapshot(self, monkeypatch, capsys):
        monkeypatch.setattr(doctor_module, "run_checks", lambda **kwargs: ALL_PASS_RESULT)
        code = doctor_module.run(_namespace(json_flag=False), db_path="ignored", now=None)
        out = capsys.readouterr().out

        assert code == 0
        assert out == (
            "ARGUS DOCTOR\n\n"
            "Configuration        PASS\n"
            "Database             PASS\n"
            "Docker connection    PASS\n"
            "Docker read access   PASS\n"
            "Collector heartbeat  PASS\n"
            "Clock                PASS\n\n"
            "Argus is operational.\n"
        )

    def test_mixed_result_snapshot(self, monkeypatch, capsys):
        monkeypatch.setattr(doctor_module, "run_checks", lambda **kwargs: MIXED_RESULT)
        code = doctor_module.run(_namespace(json_flag=False), db_path="ignored", now=None)
        out = capsys.readouterr().out

        assert code == 1
        assert out == (
            "ARGUS DOCTOR\n\n"
            "Configuration        PASS\n"
            "Database             PASS\n"
            "Docker connection    FAIL\n"
            "Docker read access   SKIP\n"
            "Collector heartbeat  WARN\n"
            "Clock                PASS\n\n"
            "Problems detected:\n\n"
            "Docker connection\n"
            "  Cannot reach the configured Docker daemon.\n\n"
            "Collector heartbeat\n"
            "  collector is currently failing (2 consecutive failures), but last success "
            "(30s ago) is still within the freshness window\n\n"
            "Argus is not fully operational.\n"
        )

    def test_warn_only_says_degraded_not_fully_healthy_or_broken(self, monkeypatch, capsys):
        monkeypatch.setattr(doctor_module, "run_checks", lambda **kwargs: WARN_ONLY_RESULT)
        code = doctor_module.run(_namespace(json_flag=False), db_path="ignored", now=None)
        out = capsys.readouterr().out

        assert code == 0  # WARN alone is still exit 0
        assert "Argus is operational, but degraded." in out
        assert "not fully operational" not in out

    def test_skip_is_never_listed_under_problems_detected(self, monkeypatch, capsys):
        monkeypatch.setattr(doctor_module, "run_checks", lambda **kwargs: MIXED_RESULT)
        doctor_module.run(_namespace(json_flag=False), db_path="ignored", now=None)
        out = capsys.readouterr().out
        # "Docker read access" (SKIP) must not appear inside the Problems section
        problems_section = out.split("Problems detected:")[1]
        assert "Docker read access" not in problems_section


class TestJsonFormatting:
    def test_json_shape_and_parseability(self, monkeypatch, capsys):
        monkeypatch.setattr(doctor_module, "run_checks", lambda **kwargs: MIXED_RESULT)
        code = doctor_module.run(_namespace(json_flag=True), db_path="ignored", now=None)
        payload = json.loads(capsys.readouterr().out)

        assert code == 1
        assert payload == {
            "operational": False,
            "checks": [
                {"name": "configuration", "status": "PASS", "message": None},
                {"name": "database", "status": "PASS", "message": None},
                {
                    "name": "docker_connection", "status": "FAIL",
                    "message": "Cannot reach the configured Docker daemon.",
                },
                {
                    "name": "docker_read_access", "status": "SKIP",
                    "message": "skipped: Docker connection failed",
                },
                {
                    "name": "collector_heartbeat", "status": "WARN",
                    "message": (
                        "collector is currently failing (2 consecutive failures), but last "
                        "success (30s ago) is still within the freshness window"
                    ),
                },
                {"name": "clock", "status": "PASS", "message": None},
            ],
        }

    def test_json_has_no_ansi_or_relative_time_field_names(self, monkeypatch, capsys):
        monkeypatch.setattr(doctor_module, "run_checks", lambda **kwargs: ALL_PASS_RESULT)
        doctor_module.run(_namespace(json_flag=True), db_path="ignored", now=None)
        raw = capsys.readouterr().out
        assert "\x1b[" not in raw  # no ANSI escape codes


# --------------------------------------------------------------------------
# Architecture / read-only guard, CLI layer
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
    def test_doctor_cli_module_has_no_forbidden_imports(self):
        source = inspect.getsource(doctor_module)
        found = _imported_roots(source) & FORBIDDEN_IMPORT_ROOTS
        assert not found

    def test_doctor_cli_module_has_no_mutating_docker_calls(self):
        source = inspect.getsource(doctor_module)
        found = [p for p in _MUTATING_CALL_PATTERNS if p in source]
        assert not found, f"doctor CLI module contains mutating Docker call(s): {found}"

    def test_doctor_cli_module_never_writes_to_the_database(self):
        source = inspect.getsource(doctor_module)
        tree = ast.parse(source)
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    called.add(func.attr)
        forbidden = {
            "insert_transition", "insert_observation", "upsert_application", "upsert_service",
            "upsert_container", "persist_discovery", "record_tick_started", "record_tick_success",
            "record_tick_failure", "open_incident", "resolve_incident", "update_incident_worst_status",
        }
        assert not (called & forbidden)
