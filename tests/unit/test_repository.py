"""Tests for argus.store.database / argus.store.repository.

All tests use file-backed temporary databases (via pytest's `tmp_path`)
rather than `:memory:` -- both because several tests require closing
and reopening the connection, and because WAL mode has no meaningful
effect on an in-memory database.
"""

from __future__ import annotations

import ast
import inspect
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from argus.domain.models import (
    Application,
    Container,
    DockerHealth,
    DockerState,
    HealthStatus,
    Observation,
    PortBinding,
    Protocol,
    Service,
)
from argus.store import database as database_module
from argus.store import repository as repository_module
from argus.store.database import (
    DatabaseOpenError,
    DuplicateObservationError,
    SchemaError,
    open_database,
)
from argus.store.repository import Repository, resolve_observation_health

UTC = timezone.utc
T1 = datetime(2026, 8, 21, 10, 0, 0, tzinfo=UTC)
T2 = datetime(2026, 8, 21, 10, 1, 0, tzinfo=UTC)
T3 = datetime(2026, 8, 21, 10, 2, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Synthetic domain object builders
# --------------------------------------------------------------------------


def make_container(
    container_id: str,
    name: str,
    *,
    compose_project: str | None = "cnstrct",
    compose_service: str | None = "api",
    image: str = "cnstrct/api:latest",
    observed_at: datetime = T1,
) -> Container:
    return Container(
        container_id=container_id,
        name=name,
        image=image,
        compose_project=compose_project,
        compose_service=compose_service,
        first_seen_at=observed_at,
        last_seen_at=observed_at,
    )


def make_observation(
    container: Container,
    *,
    observed_at: datetime = T1,
    docker_state: DockerState = DockerState.RUNNING,
    docker_health: DockerHealth | None = DockerHealth.HEALTHY,
    restart_count: int = 0,
    exit_code: int | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    ports: tuple[PortBinding, ...] = (),
    labels: dict | None = None,
    derived_status: HealthStatus = HealthStatus.HEALTHY,
    derived_detail: str | None = None,
) -> Observation:
    return Observation(
        container_ref=container,
        observed_at=observed_at,
        docker_state=docker_state,
        docker_health=docker_health,
        restart_count=restart_count,
        exit_code=exit_code,
        started_at=started_at,
        finished_at=finished_at,
        ports=ports,
        labels=labels or {},
        derived_status=derived_status,
        derived_detail=derived_detail,
    )


def make_service(
    containers: list[Container],
    *,
    application_key: str = "cnstrct",
    compose_service: str | None = "api",
    derived_status: HealthStatus = HealthStatus.HEALTHY,
) -> Service:
    return Service(
        application_key=application_key,
        compose_service=compose_service,
        containers=tuple(containers),
        derived_status=derived_status,
    )


def make_application(
    services: list[Service],
    *,
    key: str = "cnstrct",
    name: str = "CNSTRCT",
    is_standalone: bool = False,
    derived_status: HealthStatus = HealthStatus.HEALTHY,
) -> Application:
    return Application(
        key=key,
        name=name,
        is_standalone=is_standalone,
        services=tuple(services),
        derived_status=derived_status,
    )


# --------------------------------------------------------------------------
# Schema bootstrap
# --------------------------------------------------------------------------


class TestSchemaBootstrap:
    def test_fresh_database_creates_expected_tables(self, tmp_path):
        conn = open_database(tmp_path / "argus.db")
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {"applications", "services", "containers", "observations"} <= tables
        conn.close()

    def test_schema_version_is_8(self, tmp_path):
        # Was version 1 as of Milestone 4, then 2 as of Milestone 5, then 3
        # as of Milestone 6 (health_transitions/incidents), then 4 as of
        # Milestone 10 (evidence tables), then 5 as of Milestone 12
        # (incident_explanations); Milestone 12.1 bumps it again to add
        # multi-provider support (see TestSchemaMigrationV5ToV6 below).
        conn = open_database(tmp_path / "argus.db")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8  # SCHEMA_VERSION moved to 8 in Milestone 16 (multi-host agents)
        conn.close()

    def test_wal_enabled_for_file_backed_database(self, tmp_path):
        conn = open_database(tmp_path / "argus.db")
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        conn.close()


# --------------------------------------------------------------------------
# Schema migration v3 -> v4 (Milestone 10 -- evidence tables)
# --------------------------------------------------------------------------


class TestSchemaMigrationV3ToV4:
    def test_fresh_database_has_evidence_tables_and_columns(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8  # SCHEMA_VERSION moved to 8 in Milestone 16 (multi-host agents)
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {"log_cursors", "log_signals", "incident_evidence"} <= tables
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(collector_state)")}
        assert {"last_evidence_success_at", "consecutive_evidence_failures", "last_evidence_error"} <= columns
        conn.close()

    def test_existing_v3_database_migrates_without_losing_data(self, tmp_path):
        db_path = tmp_path / "a.db"

        # A genuine pre-Milestone-10 (Milestone 6) database: everything
        # through health_transitions/incidents, no evidence tables, no
        # evidence columns on collector_state, user_version=3.
        v3_conn = sqlite3.connect(str(db_path))
        v3_conn.executescript(
            """
            CREATE TABLE applications (
                id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                is_standalone INTEGER NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
            );
            CREATE TABLE collector_state (
                id INTEGER PRIMARY KEY CHECK (id = 1), last_tick_at TEXT, last_success_at TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0, last_error TEXT
            );
            """
        )
        v3_conn.execute(
            "INSERT INTO applications (key, name, is_standalone, first_seen_at, last_seen_at) "
            "VALUES ('cnstrct','CNSTRCT',0,?,?)",
            (T1.isoformat(), T1.isoformat()),
        )
        v3_conn.execute("PRAGMA user_version = 3")
        v3_conn.commit()
        v3_conn.close()

        conn = open_database(db_path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8  # SCHEMA_VERSION moved to 8 in Milestone 16 (multi-host agents)
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {"log_cursors", "log_signals", "incident_evidence"} <= tables
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(collector_state)")}
        assert {"last_evidence_success_at", "consecutive_evidence_failures", "last_evidence_error"} <= columns

        row = conn.execute("SELECT * FROM applications WHERE key = 'cnstrct'").fetchone()
        assert row["name"] == "CNSTRCT"
        conn.close()

    def test_existing_v4_database_opens_safely(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        Repository(conn).upsert_application(key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1)
        conn.close()

        conn2 = open_database(db_path)
        assert conn2.execute("PRAGMA user_version").fetchone()[0] == 8  # SCHEMA_VERSION moved to 8 in Milestone 16 (multi-host agents)
        assert Repository(conn2).get_application("cnstrct") is not None
        conn2.close()

    def test_foreign_keys_enabled(self, tmp_path):
        conn = open_database(tmp_path / "argus.db")
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        conn.close()

    def test_foreign_key_violation_is_rejected(self, tmp_path):
        conn = open_database(tmp_path / "argus.db")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO containers (service_id, container_id, name, first_seen_at, last_seen_at) "
                "VALUES (9999, 'ghost', 'ghost', ?, ?)",
                (T1.isoformat(), T1.isoformat()),
            )
        conn.close()

    def test_reopen_preserves_data_without_recreating_tables(self, tmp_path):
        db_path = tmp_path / "argus.db"
        conn = open_database(db_path)
        Repository(conn).upsert_application(
            key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1
        )
        conn.close()

        conn2 = open_database(db_path)
        record = Repository(conn2).get_application("cnstrct")
        assert record is not None
        assert record.name == "CNSTRCT"
        conn2.close()

    def test_incompatible_future_schema_version_raises(self, tmp_path):
        db_path = tmp_path / "argus.db"
        conn = open_database(db_path)
        conn.execute("PRAGMA user_version = 99")
        conn.close()

        with pytest.raises(SchemaError):
            open_database(db_path)

    def test_open_database_error_is_typed(self, tmp_path):
        # a path whose parent directory doesn't exist -- sqlite3 cannot create it
        bogus_path = tmp_path / "no-such-directory" / "argus.db"
        with pytest.raises(DatabaseOpenError):
            open_database(bogus_path)


# --------------------------------------------------------------------------
# Schema migration v4 -> v5 (Milestone 12 -- incident_explanations)
# --------------------------------------------------------------------------


class TestSchemaMigrationV4ToV5:
    def test_fresh_database_has_incident_explanations_table(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8  # SCHEMA_VERSION moved to 8 in Milestone 16 (multi-host agents)
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "incident_explanations" in tables
        conn.close()

    def test_existing_v4_database_migrates_without_losing_data(self, tmp_path):
        db_path = tmp_path / "a.db"

        # A genuine pre-Milestone-12 (Milestone 10) database: everything
        # through log_signals/incident_evidence, no incident_explanations,
        # user_version=4.
        v4_conn = sqlite3.connect(str(db_path))
        v4_conn.executescript(
            """
            CREATE TABLE applications (
                id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                is_standalone INTEGER NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
            );
            """
        )
        v4_conn.execute(
            "INSERT INTO applications (key, name, is_standalone, first_seen_at, last_seen_at) "
            "VALUES ('cnstrct','CNSTRCT',0,?,?)",
            (T1.isoformat(), T1.isoformat()),
        )
        v4_conn.execute("PRAGMA user_version = 4")
        v4_conn.commit()
        v4_conn.close()

        conn = open_database(db_path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8  # SCHEMA_VERSION moved to 8 in Milestone 16 (multi-host agents)
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "incident_explanations" in tables

        row = conn.execute("SELECT * FROM applications WHERE key = 'cnstrct'").fetchone()
        assert row["name"] == "CNSTRCT"
        conn.close()

    def test_existing_v5_database_opens_safely(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        Repository(conn).upsert_application(key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1)
        conn.close()

        conn2 = open_database(db_path)
        assert conn2.execute("PRAGMA user_version").fetchone()[0] == 8  # SCHEMA_VERSION moved to 8 in Milestone 16 (multi-host agents)
        assert Repository(conn2).get_application("cnstrct") is not None
        conn2.close()


# --------------------------------------------------------------------------
# Schema migration v5 -> v6 (Milestone 12.1 -- multi-provider AI)
# --------------------------------------------------------------------------


class TestSchemaMigrationV5ToV6:
    def test_fresh_database_has_provider_column_and_new_unique_index(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8  # SCHEMA_VERSION moved to 8 in Milestone 16 (multi-host agents)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(incident_explanations)")}
        assert "provider" in columns
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(incident_explanations)")}
        assert "ux_incident_explanations_cache_key" in indexes
        conn.close()

    def test_existing_v5_database_migrates_without_losing_data_and_backfills_anthropic(self, tmp_path):
        db_path = tmp_path / "a.db"

        # A genuine pre-Milestone-12.1 (Milestone 12) database: the old
        # incident_explanations shape, inline UNIQUE constraint, no
        # `provider` column, user_version=5.
        v5_conn = sqlite3.connect(str(db_path))
        v5_conn.executescript(
            """
            CREATE TABLE applications (
                id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                is_standalone INTEGER NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
            );
            CREATE TABLE incidents (
                id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT 'application', scope_id INTEGER NOT NULL,
                failure_signature TEXT NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT, status TEXT NOT NULL,
                opening_status TEXT NOT NULL, worst_status TEXT NOT NULL, opening_transition_id INTEGER NOT NULL,
                resolving_transition_id INTEGER
            );
            CREATE TABLE incident_explanations (
                id INTEGER PRIMARY KEY,
                incident_id INTEGER NOT NULL REFERENCES incidents(id),
                bundle_fingerprint TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                summary TEXT NOT NULL,
                root_cause TEXT,
                confidence TEXT NOT NULL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                response_json TEXT NOT NULL,
                UNIQUE (incident_id, bundle_fingerprint, model, prompt_version)
            );
            """
        )
        v5_conn.execute(
            "INSERT INTO applications (key, name, is_standalone, first_seen_at, last_seen_at) "
            "VALUES ('cnstrct','CNSTRCT',0,?,?)",
            (T1.isoformat(), T1.isoformat()),
        )
        v5_conn.execute(
            "INSERT INTO incidents (id, scope_id, failure_signature, opened_at, status, opening_status, "
            "worst_status, opening_transition_id) VALUES (1, 1, 'application:cnstrct', ?, 'open', "
            "'UNHEALTHY', 'UNHEALTHY', 1)",
            (T1.isoformat(),),
        )
        v5_conn.execute(
            "INSERT INTO incident_explanations (incident_id, bundle_fingerprint, model, prompt_version, "
            "created_at, summary, confidence, response_json) VALUES "
            "(1, 'fp1', 'claude-sonnet-5', 'incident-explanation-v1', ?, 'legacy summary', 'medium', '{}')",
            (T1.isoformat(),),
        )
        v5_conn.execute("PRAGMA user_version = 5")
        v5_conn.commit()
        v5_conn.close()

        conn = open_database(db_path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8  # SCHEMA_VERSION moved to 8 in Milestone 16 (multi-host agents)
        row = conn.execute(
            "SELECT incident_id, bundle_fingerprint, provider, model, summary FROM incident_explanations WHERE id = 1"
        ).fetchone()
        assert row["provider"] == "anthropic"  # backfilled -- Gemini didn't exist before this migration
        assert row["summary"] == "legacy summary"  # existing data preserved

        app_row = conn.execute("SELECT * FROM applications WHERE key = 'cnstrct'").fetchone()
        assert app_row["name"] == "CNSTRCT"
        conn.close()

    def test_migrated_database_allows_a_different_provider_for_the_same_key(self, tmp_path):
        """The whole point of the migration: a Gemini explanation and the
        legacy Anthropic explanation for the same
        (incident, bundle_fingerprint, model, prompt_version) must
        coexist -- the old inline UNIQUE constraint would have forbidden
        this."""

        db_path = tmp_path / "a.db"
        v5_conn = sqlite3.connect(str(db_path))
        v5_conn.executescript(
            """
            CREATE TABLE applications (
                id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                is_standalone INTEGER NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
            );
            CREATE TABLE incidents (
                id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT 'application', scope_id INTEGER NOT NULL,
                failure_signature TEXT NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT, status TEXT NOT NULL,
                opening_status TEXT NOT NULL, worst_status TEXT NOT NULL, opening_transition_id INTEGER NOT NULL,
                resolving_transition_id INTEGER
            );
            CREATE TABLE incident_explanations (
                id INTEGER PRIMARY KEY,
                incident_id INTEGER NOT NULL REFERENCES incidents(id),
                bundle_fingerprint TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                summary TEXT NOT NULL,
                root_cause TEXT,
                confidence TEXT NOT NULL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                response_json TEXT NOT NULL,
                UNIQUE (incident_id, bundle_fingerprint, model, prompt_version)
            );
            """
        )
        v5_conn.execute(
            "INSERT INTO applications (key, name, is_standalone, first_seen_at, last_seen_at) "
            "VALUES ('cnstrct','CNSTRCT',0,?,?)", (T1.isoformat(), T1.isoformat()),
        )
        v5_conn.execute(
            "INSERT INTO incidents (id, scope_id, failure_signature, opened_at, status, opening_status, "
            "worst_status, opening_transition_id) VALUES (1, 1, 'application:cnstrct', ?, 'open', "
            "'UNHEALTHY', 'UNHEALTHY', 1)", (T1.isoformat(),),
        )
        v5_conn.execute(
            "INSERT INTO incident_explanations (incident_id, bundle_fingerprint, model, prompt_version, "
            "created_at, summary, confidence, response_json) VALUES "
            "(1, 'fp1', 'claude-sonnet-5', 'incident-explanation-v1', ?, 'claude summary', 'medium', '{}')",
            (T1.isoformat(),),
        )
        v5_conn.execute("PRAGMA user_version = 5")
        v5_conn.commit()
        v5_conn.close()

        conn = open_database(db_path)
        repo = Repository(conn)
        # same incident, fingerprint, model, prompt_version as the legacy
        # row -- only the provider differs.
        new_id = repo.save_explanation(
            incident_id=1, bundle_fingerprint="fp1", provider="gemini", model="claude-sonnet-5",
            prompt_version="incident-explanation-v1", created_at=T1, summary="gemini summary",
            root_cause=None, confidence="medium", input_tokens=None, output_tokens=None, response_json="{}",
        )
        assert new_id is not None
        history = repo.list_explanations_for_incident(1)
        assert {row.provider for row in history} == {"anthropic", "gemini"}
        conn.close()


# --------------------------------------------------------------------------
# Schema migration v7 -> v8 (Milestone 16 -- multi-host agents)
# --------------------------------------------------------------------------

#: Every column `Repository`/ingestion actually read/write against
#: `hosts` (see `argus.store.repository._row_to_host_record`,
#: `Repository.ensure_local_host`, and `Repository.create_agent_host`).
_EXPECTED_HOST_COLUMNS = {
    "id",
    "host_key",
    "agent_id",
    "display_name",
    "kind",
    "agent_token_hash",
    "agent_version",
    "first_seen_at",
    "last_seen_at",
}


class TestSchemaMigrationV7ToV8:
    def test_fresh_database_hosts_table_has_every_expected_column(self, tmp_path):
        """Schema-shape check: a brand-new (never-migrated) database's
        `hosts` table must carry every column `Repository`/ingestion
        expect -- independent of the migration path entirely, since a
        fresh database never runs `_migrate_v7_to_v8` at all (see the
        `current_version == 0` branch in `initialize_database`)."""

        conn = open_database(tmp_path / "a.db")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(hosts)")}
        assert _EXPECTED_HOST_COLUMNS <= columns
        conn.close()

    def test_migrated_database_hosts_table_has_every_expected_column(self, tmp_path):
        """Same schema-shape check, but for a database that actually
        went through `_migrate_v7_to_v8` -- the migration path must
        converge on exactly the same column set a fresh database gets,
        not merely "enough columns to not crash on the one INSERT that
        happened to be tried"."""

        db_path = tmp_path / "a.db"
        v7_conn = sqlite3.connect(str(db_path))
        v7_conn.executescript(
            """
            CREATE TABLE applications (
                id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                is_standalone INTEGER NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
            );
            """
        )
        v7_conn.execute("PRAGMA user_version = 7")
        v7_conn.commit()
        v7_conn.close()

        conn = open_database(db_path)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(hosts)")}
        assert _EXPECTED_HOST_COLUMNS <= columns
        conn.close()

    def test_v7_database_with_legacy_hosts_table_missing_agent_id_migrates_successfully(self, tmp_path):
        """The exact real-world bug: a v7 database whose `hosts` table
        already exists (e.g. left over from an in-progress build of
        Milestone 16 that created the table -- and inserted the local
        host row -- before `agent_id` existed in `schema.sql`) but does
        NOT have `agent_id` (or the other columns that were added to
        `hosts` alongside it). `schema.sql`'s own `CREATE TABLE IF NOT
        EXISTS hosts` is a no-op against this table, so
        `_migrate_v7_to_v8` must repair its shape itself before
        `_ensure_local_host` ever tries to INSERT into it -- see
        `_ensure_hosts_columns`.

        Before the fix, opening a database in exactly this shape raised
        ``sqlite3.OperationalError: table hosts has no column named
        agent_id`` from inside `_ensure_local_host`'s INSERT.
        """

        db_path = tmp_path / "a.db"
        v7_conn = sqlite3.connect(str(db_path))
        v7_conn.executescript(
            """
            CREATE TABLE applications (
                id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                is_standalone INTEGER NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
            );
            CREATE TABLE services (
                id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id),
                compose_service TEXT, service_key TEXT NOT NULL, name TEXT NOT NULL,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                UNIQUE (application_id, service_key)
            );
            CREATE TABLE containers (
                id INTEGER PRIMARY KEY, service_id INTEGER NOT NULL REFERENCES services(id),
                container_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
            );
            CREATE TABLE observations (
                id INTEGER PRIMARY KEY, container_id INTEGER NOT NULL REFERENCES containers(id),
                observed_at TEXT NOT NULL, docker_state TEXT NOT NULL, docker_health TEXT,
                restart_count INTEGER NOT NULL, exit_code INTEGER, started_at TEXT, finished_at TEXT,
                image TEXT NOT NULL, ports_json TEXT NOT NULL, labels_json TEXT NOT NULL,
                UNIQUE (container_id, observed_at)
            );
            -- The legacy hosts table shape: has `host_key`, but predates
            -- `agent_id`/`agent_token_hash`/`agent_version` entirely --
            -- the exact shape a partially-built Milestone 16 database
            -- was left in.
            CREATE TABLE hosts (
                id INTEGER PRIMARY KEY,
                host_key TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                kind TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            """
        )
        v7_conn.execute(
            "INSERT INTO applications (key, name, is_standalone, first_seen_at, last_seen_at) "
            "VALUES ('cnstrct','CNSTRCT',0,?,?)",
            (T1.isoformat(), T1.isoformat()),
        )
        v7_conn.execute(
            "INSERT INTO services (application_id, compose_service, service_key, name, first_seen_at, last_seen_at) "
            "VALUES (1, 'api', 'api', 'api', ?, ?)",
            (T1.isoformat(), T1.isoformat()),
        )
        v7_conn.execute(
            "INSERT INTO containers (service_id, container_id, name, first_seen_at, last_seen_at) "
            "VALUES (1, 'abc123', 'cnstrct-api-1', ?, ?)",
            (T1.isoformat(), T1.isoformat()),
        )
        v7_conn.execute(
            "INSERT INTO observations (container_id, observed_at, docker_state, restart_count, image, "
            "ports_json, labels_json) VALUES (1, ?, 'running', 0, 'cnstrct/api:latest', '[]', '{}')",
            (T1.isoformat(),),
        )
        # The pre-existing local host row -- inserted back when `hosts`
        # had no `agent_id` column at all.
        v7_conn.execute(
            "INSERT INTO hosts (host_key, display_name, kind, first_seen_at, last_seen_at) "
            "VALUES ('local', 'Local Host', 'local', ?, ?)",
            (T1.isoformat(), T1.isoformat()),
        )
        v7_conn.execute("PRAGMA user_version = 7")
        v7_conn.commit()
        v7_conn.close()

        # Must not raise -- this is the exact call that raised
        # `sqlite3.OperationalError` before the fix.
        conn = open_database(db_path)

        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8

        host_columns = {row["name"] for row in conn.execute("PRAGMA table_info(hosts)")}
        assert _EXPECTED_HOST_COLUMNS <= host_columns

        # Local host backfill: still exactly one 'local' row, its
        # pre-existing identity (display_name/kind/timestamps) untouched,
        # `agent_id` present and NULL (never had a value to preserve).
        host_rows = conn.execute("SELECT * FROM hosts WHERE host_key = 'local'").fetchall()
        assert len(host_rows) == 1
        local_host = host_rows[0]
        assert local_host["agent_id"] is None
        assert local_host["display_name"] == "Local Host"
        assert local_host["kind"] == "local"

        # Existing application/service/container/observation history
        # preserved, and backfilled to the (same) local host.
        app_row = conn.execute("SELECT * FROM applications WHERE key = 'cnstrct'").fetchone()
        assert app_row["name"] == "CNSTRCT"
        assert app_row["host_id"] == local_host["id"]

        container_row = conn.execute("SELECT * FROM containers WHERE container_id = 'abc123'").fetchone()
        assert container_row["name"] == "cnstrct-api-1"
        assert container_row["host_id"] == local_host["id"]

        observation_row = conn.execute("SELECT * FROM observations WHERE container_id = 1").fetchone()
        assert observation_row["docker_state"] == "running"

        # Repository-level read confirms the row is actually usable,
        # not just structurally present.
        repo = Repository(conn)
        record = repo.get_host_by_key("local")
        assert record is not None
        assert record.kind == "local"
        conn.close()

    def test_migration_is_idempotent_on_reopen(self, tmp_path):
        """Reopening a database already migrated past this bug must not
        re-raise, re-insert a second local host row, or otherwise
        misbehave -- consistent with `open_database`'s own documented
        idempotency guarantee."""

        db_path = tmp_path / "a.db"
        v7_conn = sqlite3.connect(str(db_path))
        v7_conn.executescript(
            """
            CREATE TABLE applications (
                id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                is_standalone INTEGER NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
            );
            CREATE TABLE hosts (
                id INTEGER PRIMARY KEY,
                host_key TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                kind TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            """
        )
        v7_conn.execute(
            "INSERT INTO hosts (host_key, display_name, kind, first_seen_at, last_seen_at) "
            "VALUES ('local', 'Local Host', 'local', ?, ?)",
            (T1.isoformat(), T1.isoformat()),
        )
        v7_conn.execute("PRAGMA user_version = 7")
        v7_conn.commit()
        v7_conn.close()

        conn = open_database(db_path)
        conn.close()

        # Reopen -- must not raise, and must not duplicate the local host.
        conn2 = open_database(db_path)
        assert conn2.execute("PRAGMA user_version").fetchone()[0] == 8
        host_rows = conn2.execute("SELECT id FROM hosts WHERE host_key = 'local'").fetchall()
        assert len(host_rows) == 1
        conn2.close()


# --------------------------------------------------------------------------
# Application identity
# --------------------------------------------------------------------------


class TestApplicationIdentity:
    def test_insert_new(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        row_id = repo.upsert_application(
            key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1
        )
        record = repo.get_application("cnstrct")
        assert record.id == row_id
        assert record.first_seen_at == T1
        assert record.last_seen_at == T1
        assert record.is_standalone is False
        conn.close()

    def test_repeated_key_preserves_first_seen_and_advances_last_seen(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        id1 = repo.upsert_application(
            key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1
        )
        id2 = repo.upsert_application(
            key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T2
        )
        assert id1 == id2
        record = repo.get_application("cnstrct")
        assert record.first_seen_at == T1
        assert record.last_seen_at == T2
        conn.close()

    def test_out_of_order_observed_at_does_not_rewind_last_seen(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        repo.upsert_application(key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T2)
        repo.upsert_application(key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1)
        record = repo.get_application("cnstrct")
        assert record.last_seen_at == T2  # not rewound to the earlier T1
        conn.close()

    def test_name_is_current_metadata(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        repo.upsert_application(key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1)
        repo.upsert_application(
            key="cnstrct", name="CNSTRCT (renamed)", is_standalone=False, observed_at=T2
        )
        record = repo.get_application("cnstrct")
        assert record.name == "CNSTRCT (renamed)"
        conn.close()

    def test_unknown_key_returns_none(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        assert Repository(conn).get_application("does-not-exist") is None
        conn.close()


# --------------------------------------------------------------------------
# Service identity
# --------------------------------------------------------------------------


class TestServiceIdentity:
    def test_insert_new_and_repeat_advances_last_seen(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app_id = repo.upsert_application(
            key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1
        )
        id1 = repo.upsert_service(
            application_id=app_id, compose_service="api", name="api", observed_at=T1
        )
        id2 = repo.upsert_service(
            application_id=app_id, compose_service="api", name="api", observed_at=T2
        )
        assert id1 == id2
        services = repo.get_services_for_application(app_id)
        assert len(services) == 1
        assert services[0].first_seen_at == T1
        assert services[0].last_seen_at == T2
        conn.close()

    def test_standalone_null_compose_service_is_unique_per_application(self, tmp_path):
        """Guards against SQLite's NULL-uniqueness quirk: a naive
        UNIQUE(application_id, compose_service) would let this insert twice."""
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app_id = repo.upsert_application(
            key="standalone:twingate-connector",
            name="twingate-connector",
            is_standalone=True,
            observed_at=T1,
        )
        id1 = repo.upsert_service(
            application_id=app_id, compose_service=None, name="twingate-connector", observed_at=T1
        )
        id2 = repo.upsert_service(
            application_id=app_id, compose_service=None, name="twingate-connector", observed_at=T2
        )
        assert id1 == id2
        services = repo.get_services_for_application(app_id)
        assert len(services) == 1
        assert services[0].compose_service is None
        conn.close()

    def test_different_services_in_same_application_are_distinct(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app_id = repo.upsert_application(
            key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1
        )
        repo.upsert_service(application_id=app_id, compose_service="api", name="api", observed_at=T1)
        repo.upsert_service(
            application_id=app_id, compose_service="postgres", name="postgres", observed_at=T1
        )
        services = repo.get_services_for_application(app_id)
        assert {s.compose_service for s in services} == {"api", "postgres"}
        conn.close()


# --------------------------------------------------------------------------
# Container identity
# --------------------------------------------------------------------------


class TestContainerIdentity:
    def _app_and_service(self, repo: Repository) -> int:
        app_id = repo.upsert_application(
            key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1
        )
        return repo.upsert_service(
            application_id=app_id, compose_service="api", name="api", observed_at=T1
        )

    def test_insert_new(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        service_id = self._app_and_service(repo)
        row_id = repo.upsert_container(
            service_id=service_id,
            container_id="AAA",
            name="cnstrct-api-1",
            first_seen_at=T1,
            last_seen_at=T1,
        )
        record = repo.get_container_by_docker_id("AAA")
        assert record.id == row_id
        conn.close()

    def test_container_recreation_preserves_old_row_and_creates_new_row(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        service_id = self._app_and_service(repo)

        old_id = repo.upsert_container(
            service_id=service_id,
            container_id="AAA",
            name="cnstrct-api-1",
            first_seen_at=T1,
            last_seen_at=T1,
        )
        new_id = repo.upsert_container(
            service_id=service_id,
            container_id="BBB",
            name="cnstrct-api-1",  # same Docker name, different container_id
            first_seen_at=T2,
            last_seen_at=T2,
        )

        assert old_id != new_id
        containers = repo.get_containers_for_service(service_id)
        assert {c.container_id for c in containers} == {"AAA", "BBB"}
        assert len(containers) == 2  # neither overwrote the other
        assert repo.get_container_by_docker_id("AAA").first_seen_at == T1
        assert repo.get_container_by_docker_id("BBB").first_seen_at == T2
        conn.close()

    def test_name_update_for_same_container_id_keeps_same_identity(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        service_id = self._app_and_service(repo)

        id1 = repo.upsert_container(
            service_id=service_id,
            container_id="AAA",
            name="old-name",
            first_seen_at=T1,
            last_seen_at=T1,
        )
        id2 = repo.upsert_container(
            service_id=service_id,
            container_id="AAA",
            name="new-name",
            first_seen_at=T2,
            last_seen_at=T2,
        )

        assert id1 == id2  # same identity, not a new row
        record = repo.get_container_by_docker_id("AAA")
        assert record.name == "new-name"
        assert record.first_seen_at == T1  # never moved forward
        assert record.last_seen_at == T2
        conn.close()


# --------------------------------------------------------------------------
# Observation round trip
# --------------------------------------------------------------------------


class TestObservationRoundTrip:
    def _container_row(self, repo: Repository, container_id: str = "AAA") -> int:
        app_id = repo.upsert_application(
            key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1
        )
        service_id = repo.upsert_service(
            application_id=app_id, compose_service="api", name="api", observed_at=T1
        )
        return repo.upsert_container(
            service_id=service_id,
            container_id=container_id,
            name="cnstrct-api-1",
            first_seen_at=T1,
            last_seen_at=T1,
        )

    def test_full_round_trip_with_healthy(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        container_row_id = self._container_row(repo)

        container = make_container("AAA", "cnstrct-api-1")
        observation = make_observation(
            container,
            observed_at=T1,
            docker_state=DockerState.RUNNING,
            docker_health=DockerHealth.HEALTHY,
            restart_count=2,
            exit_code=None,
            started_at=T1,
            finished_at=None,
            ports=(
                PortBinding(container_port=8080, protocol=Protocol.TCP, host_ip="0.0.0.0", host_port=8080),
            ),
            labels={"com.docker.compose.project": "cnstrct", "com.docker.compose.service": "api"},
            derived_status=HealthStatus.HEALTHY,
            derived_detail=None,
        )
        repo.insert_observation(container_row_id=container_row_id, observation=observation)
        conn.close()

        conn2 = open_database(db_path)
        reloaded = Repository(conn2).get_latest_observation("AAA")
        conn2.close()

        assert reloaded == observation

    def test_round_trip_with_none_values(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        container_row_id = self._container_row(repo)

        container = make_container("AAA", "cnstrct-api-1")
        observation = make_observation(
            container,
            docker_health=None,
            exit_code=None,
            started_at=None,
            finished_at=None,
            ports=(PortBinding(container_port=5432, protocol=Protocol.TCP, host_ip=None, host_port=None),),
            derived_detail=None,
        )
        repo.insert_observation(container_row_id=container_row_id, observation=observation)
        conn.close()

        conn2 = open_database(db_path)
        reloaded = Repository(conn2).get_latest_observation("AAA")
        conn2.close()

        assert reloaded == observation
        assert reloaded.docker_health is None
        assert reloaded.exit_code is None
        assert reloaded.started_at is None
        assert reloaded.finished_at is None
        assert reloaded.ports[0].host_ip is None
        assert reloaded.ports[0].host_port is None
        assert reloaded.derived_detail is None

    def test_multiple_observations_read_back_in_chronological_order(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        container_row_id = self._container_row(repo)
        container = make_container("AAA", "cnstrct-api-1")

        for observed_at, status in (
            (T1, HealthStatus.HEALTHY),
            (T2, HealthStatus.DEGRADED),
            (T3, HealthStatus.HEALTHY),
        ):
            repo.insert_observation(
                container_row_id=container_row_id,
                observation=make_observation(container, observed_at=observed_at, derived_status=status),
            )

        history = repo.get_observation_history("AAA")
        assert [o.observed_at for o in history] == [T1, T2, T3]
        assert [o.derived_status for o in history] == [
            HealthStatus.HEALTHY,
            HealthStatus.DEGRADED,
            HealthStatus.HEALTHY,
        ]
        conn.close()

    def test_duplicate_observation_raises_and_does_not_overwrite(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        container_row_id = self._container_row(repo)
        container = make_container("AAA", "cnstrct-api-1")

        first = make_observation(container, observed_at=T1, derived_status=HealthStatus.HEALTHY)
        repo.insert_observation(container_row_id=container_row_id, observation=first)

        duplicate = make_observation(container, observed_at=T1, derived_status=HealthStatus.UNHEALTHY)
        with pytest.raises(DuplicateObservationError):
            repo.insert_observation(container_row_id=container_row_id, observation=duplicate)

        # the original row must be untouched -- append-only, not last-write-wins
        history = repo.get_observation_history("AAA")
        assert len(history) == 1
        assert history[0].derived_status is HealthStatus.HEALTHY
        conn.close()

    def test_get_latest_observation_returns_none_when_no_history(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        self._container_row(repo)
        assert repo.get_latest_observation("AAA") is None
        conn.close()


# --------------------------------------------------------------------------
# persist_discovery + transactions
# --------------------------------------------------------------------------


class TestPersistDiscoveryAndTransactions:
    def test_full_snapshot_persists_and_survives_reopen(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)

        api_container = make_container("AAA", "cnstrct-api-1", observed_at=T1)
        api_service = make_service([api_container], compose_service="api")
        pg_container = make_container(
            "BBB", "cnstrct-postgres-1", observed_at=T1, compose_service="postgres"
        )
        pg_service = make_service([pg_container], compose_service="postgres")
        app = make_application([api_service, pg_service])

        observations = [
            make_observation(api_container, observed_at=T1),
            make_observation(pg_container, observed_at=T1, docker_health=None),
        ]

        report = repo.persist_discovery(applications=[app], observations=observations)
        assert report.applications_written == 1
        assert report.services_written == 2
        assert report.containers_written == 2
        assert report.observations_written == 2
        conn.close()

        conn2 = open_database(db_path)
        repo2 = Repository(conn2)
        record = repo2.get_application("cnstrct")
        assert record is not None
        services = repo2.get_services_for_application(record.id)
        assert {s.compose_service for s in services} == {"api", "postgres"}
        conn2.close()

    def test_transaction_rolls_back_completely_on_duplicate_observation(self, tmp_path):
        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)

        container = make_container("AAA", "cnstrct-api-1", observed_at=T1)
        service = make_service([container], compose_service="api")
        app = make_application([service])
        obs = make_observation(container, observed_at=T1)
        repo.persist_discovery(applications=[app], observations=[obs])

        # second snapshot: a brand-new, never-seen application is processed
        # FIRST (so its identities really do get written inside this
        # transaction), followed by a duplicate observation for the
        # already-persisted container+timestamp above.
        new_container = make_container(
            "ZZZ", "newapp-web-1", observed_at=T2, compose_project="newapp", compose_service="web"
        )
        new_service = make_service([new_container], application_key="newapp", compose_service="web")
        new_app = make_application([new_service], key="newapp", name="newapp")

        with pytest.raises(DuplicateObservationError):
            repo.persist_discovery(
                applications=[new_app, app],
                observations=[make_observation(new_container, observed_at=T2), obs],
            )

        # the whole batch rolled back -- including the new application that
        # was successfully written earlier in the very same transaction
        assert repo.get_application("newapp") is None
        assert repo.get_container_by_docker_id("ZZZ") is None
        conn.close()

    def test_service_with_zero_containers_raises_clear_persistence_error(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        empty_service = make_service([], compose_service="ghost")
        app = make_application([empty_service])

        with pytest.raises(repository_module.PersistenceError):
            repo.persist_discovery(applications=[app], observations=[])
        conn.close()


# --------------------------------------------------------------------------
# M3 -> M4 integration: real discovery, fixture-driven, through persistence
# --------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "docker_responses"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


class _FakeContainer:
    def __init__(self, id: str, attrs: dict):
        self.id = id
        self.attrs = attrs


class _FakeContainersAPI:
    def __init__(self, by_id: dict[str, dict]):
        self._by_id = by_id

    def list(self, all=False):
        return [_FakeContainer(cid, {}) for cid in self._by_id]

    def get(self, container_id):
        return _FakeContainer(container_id, self._by_id[container_id])


class _FakeSDKClient:
    def __init__(self, by_id: dict[str, dict]):
        self.containers = _FakeContainersAPI(by_id)


def _make_real_discovery_client(fixture_names: list[str]):
    from argus.collectors.docker_client import DockerClient

    by_id = {}
    for fixture_name in fixture_names:
        attrs = _load_fixture(fixture_name)
        by_id[attrs["Id"]] = attrs
    return DockerClient(client=_FakeSDKClient(by_id))


class TestM3ToM4Integration:
    def test_real_discovery_persists_and_reloads_correctly(self, tmp_path):
        from argus.collectors.docker_collector import discover

        client = _make_real_discovery_client(
            ["compose_healthy_api", "compose_postgres_nohealthcheck", "compose_redis_unhealthy"]
        )
        observed_at = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
        result = discover(client, observed_at=observed_at)

        # bridge M3's placeholder derived_status with the real evaluated
        # health before persisting -- see resolve_observation_health's
        # docstring for why this seam exists.
        resolved_observations = [
            resolve_observation_health(
                observation,
                status=result.evaluations[observation.container_ref.container_id].status,
                detail=result.evaluations[observation.container_ref.container_id].detail,
            )
            for observation in result.observations
        ]

        db_path = tmp_path / "integration.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        report = repo.persist_discovery(
            applications=result.applications, observations=resolved_observations
        )
        conn.close()

        assert report.applications_written == len(result.applications)
        assert report.services_written == sum(len(a.services) for a in result.applications)
        assert report.containers_written == sum(
            len(s.containers) for a in result.applications for s in a.services
        )
        assert report.observations_written == len(resolved_observations)

        conn2 = open_database(db_path)
        repo2 = Repository(conn2)

        app_record = repo2.get_application("cnstrct")
        assert app_record is not None
        services = repo2.get_services_for_application(app_record.id)
        assert {s.compose_service for s in services} == {"api", "postgres", "redis"}

        for resolved in resolved_observations:
            container_id = resolved.container_ref.container_id
            reloaded = repo2.get_latest_observation(container_id)
            assert reloaded == resolved
            assert reloaded.derived_status == result.evaluations[container_id].status
            assert reloaded.ports == resolved.ports
            assert reloaded.labels == resolved.labels
            assert reloaded.observed_at == observed_at

        conn2.close()


# --------------------------------------------------------------------------
# Security: no data beyond what Milestone 3 already allowlisted
# --------------------------------------------------------------------------


class TestSecurity:
    def test_labels_json_contains_only_allowlisted_labels(self, tmp_path):
        from argus.collectors.docker_collector import discover

        client = _make_real_discovery_client(["compose_healthy_api"])
        observed_at = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
        result = discover(client, observed_at=observed_at)

        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        repo.persist_discovery(applications=result.applications, observations=result.observations)

        raw_row = conn.execute("SELECT labels_json FROM observations").fetchone()
        stored_labels = json.loads(raw_row["labels_json"])

        assert stored_labels == {
            "com.docker.compose.project": "cnstrct",
            "com.docker.compose.service": "api",
            "argus.owner": "jorge",
        }
        assert "some.secret.label" not in stored_labels
        assert "com.docker.compose.project.working_dir" not in stored_labels
        conn.close()

    def test_no_unexpected_fields_survive_into_stored_json(self, tmp_path):
        """Simulates a fixture carrying an unexpected extra field and proves
        it never reaches storage -- Argus only ever writes what Milestone 3
        already extracted onto the Observation object, nothing raw."""
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app_id = repo.upsert_application(key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=T1)
        service_id = repo.upsert_service(
            application_id=app_id, compose_service="api", name="api", observed_at=T1
        )
        container_row_id = repo.upsert_container(
            service_id=service_id,
            container_id="AAA",
            name="cnstrct-api-1",
            first_seen_at=T1,
            last_seen_at=T1,
        )
        container = make_container("AAA", "cnstrct-api-1")
        observation = make_observation(
            container, labels={"com.docker.compose.project": "cnstrct", "argus.owner": "jorge"}
        )
        repo.insert_observation(container_row_id=container_row_id, observation=observation)

        raw_row = conn.execute("SELECT labels_json, ports_json FROM observations").fetchone()
        assert set(json.loads(raw_row["labels_json"])) == {"com.docker.compose.project", "argus.owner"}
        # ports_json/labels_json only ever hold what PortBinding/labels already were --
        # never raw Docker Env, Mounts, or full Config.
        assert "Env" not in raw_row["ports_json"]
        assert "Mounts" not in raw_row["ports_json"]
        conn.close()


# --------------------------------------------------------------------------
# Evidence (schema v4, Milestone 10)
# --------------------------------------------------------------------------


def _seed_container(repo, *, key="cnstrct", name="CNSTRCT", compose_service="api", container_id="docker-abc", at=T1):
    app_id = repo.upsert_application(key=key, name=name, is_standalone=False, observed_at=at)
    svc_id = repo.upsert_service(application_id=app_id, compose_service=compose_service, name=compose_service, observed_at=at)
    container_row_id = repo.upsert_container(
        service_id=svc_id, container_id=container_id, name=f"{key}-{compose_service}-1", first_seen_at=at, last_seen_at=at
    )
    return app_id, container_row_id


class TestLogCursors:
    def test_no_cursor_yet_returns_none(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        _, container_row_id = _seed_container(repo)
        assert repo.get_log_cursor(container_row_id) is None
        conn.close()

    def test_round_trip(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        _, container_row_id = _seed_container(repo)
        repo.set_log_cursor(container_row_id, last_log_at=T1, updated_at=T1)
        assert repo.get_log_cursor(container_row_id) == T1
        conn.close()

    def test_cursor_never_moves_backward(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        _, container_row_id = _seed_container(repo)
        repo.set_log_cursor(container_row_id, last_log_at=T3, updated_at=T3)
        repo.set_log_cursor(container_row_id, last_log_at=T1, updated_at=T1)  # an out-of-order call
        assert repo.get_log_cursor(container_row_id) == T3
        conn.close()

    def test_cursor_survives_process_restart(self, tmp_path):
        """A fresh Repository/connection against the same database file
        sees exactly the same cursor -- the collector's own restart
        safety, applied to evidence collection."""

        db_path = tmp_path / "a.db"
        conn = open_database(db_path)
        repo = Repository(conn)
        _, container_row_id = _seed_container(repo)
        repo.set_log_cursor(container_row_id, last_log_at=T2, updated_at=T2)
        conn.close()

        conn2 = open_database(db_path)
        repo2 = Repository(conn2)
        assert repo2.get_log_cursor(container_row_id) == T2
        conn2.close()


class TestLogSignalsPersistence:
    def test_insert_and_read_round_trip_every_field(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app_id, container_row_id = _seed_container(repo)

        signal_id = repo.insert_log_signal(
            application_id=app_id, container_row_id=container_row_id, category="db_connection_timeout",
            severity="high", normalized_signature="connection timeout after #s", first_seen_at=T1,
            last_seen_at=T2, count=5, sample="connection timeout after 30s", source_type="container_log",
            source_ref="stdout+stderr",
        )
        signal = repo.get_log_signal(signal_id)
        assert signal.application_key == "cnstrct"
        assert signal.container_id == "docker-abc"
        assert signal.category.value == "db_connection_timeout"
        assert signal.severity.value == "high"
        assert signal.first_seen_at == T1
        assert signal.last_seen_at == T2
        assert signal.count == 5
        assert signal.sample == "connection timeout after 30s"
        assert signal.source_type == "container_log"
        assert signal.source_ref == "stdout+stderr"
        conn.close()

    def test_extend_advances_last_seen_and_count_never_touches_sample(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app_id, container_row_id = _seed_container(repo)
        signal_id = repo.insert_log_signal(
            application_id=app_id, container_row_id=container_row_id, category="oom", severity="critical",
            normalized_signature="oom killed", first_seen_at=T1, last_seen_at=T1, count=1,
            sample="oom killed", source_type="container_log", source_ref="stdout+stderr",
        )
        repo.extend_log_signal(signal_id, last_seen_at=T2, additional_count=4)
        signal = repo.get_log_signal(signal_id)
        assert signal.count == 5
        assert signal.last_seen_at == T2
        assert signal.sample == "oom killed"
        conn.close()

    def test_find_latest_log_signal_returns_none_when_nothing_matches(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        _, container_row_id = _seed_container(repo)
        found = repo.find_latest_log_signal(
            container_row_id=container_row_id, category="oom", normalized_signature="oom killed"
        )
        assert found is None
        conn.close()

    def test_list_log_signals_for_application_respects_since(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app_id, container_row_id = _seed_container(repo)
        repo.insert_log_signal(
            application_id=app_id, container_row_id=container_row_id, category="oom", severity="critical",
            normalized_signature="a", first_seen_at=T1, last_seen_at=T1, count=1, sample="a",
            source_type="container_log", source_ref="stdout+stderr",
        )
        repo.insert_log_signal(
            application_id=app_id, container_row_id=container_row_id, category="oom", severity="critical",
            normalized_signature="b", first_seen_at=T3, last_seen_at=T3, count=1, sample="b",
            source_type="container_log", source_ref="stdout+stderr",
        )
        recent_only = repo.list_log_signals_for_application(app_id, since=T2)
        assert len(recent_only) == 1
        assert recent_only[0].sample == "b"
        conn.close()


class TestRetention:
    def test_unlinked_expired_signal_is_deleted(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app_id, container_row_id = _seed_container(repo)
        old = repo.insert_log_signal(
            application_id=app_id, container_row_id=container_row_id, category="oom", severity="critical",
            normalized_signature="old", first_seen_at=T1, last_seen_at=T1, count=1, sample="old",
            source_type="container_log", source_ref="stdout+stderr",
        )
        deleted = repo.delete_expired_log_signals(before=T2)
        assert deleted == 1
        assert repo.get_log_signal(old) is None
        conn.close()

    def test_signal_linked_to_an_incident_survives_regardless_of_age(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app_id, container_row_id = _seed_container(repo)
        signal_id = repo.insert_log_signal(
            application_id=app_id, container_row_id=container_row_id, category="oom", severity="critical",
            normalized_signature="old", first_seen_at=T1, last_seen_at=T1, count=1, sample="old",
            source_type="container_log", source_ref="stdout+stderr",
        )
        transition_id = repo.insert_transition(
            scope="application", scope_id=app_id, from_status=None, to_status=HealthStatus.UNHEALTHY, occurred_at=T1
        )
        incident_id = repo.open_incident(
            scope_id=app_id, failure_signature="application:cnstrct", opened_at=T1,
            opening_status=HealthStatus.UNHEALTHY, opening_transition_id=transition_id,
        )
        repo.link_incident_evidence(incident_id=incident_id, log_signal_id=signal_id, linked_at=T1)

        deleted = repo.delete_expired_log_signals(before=T3)  # would otherwise delete it -- it's ancient
        assert deleted == 0
        assert repo.get_log_signal(signal_id) is not None
        conn.close()

    def test_recent_unlinked_signal_is_not_deleted(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app_id, container_row_id = _seed_container(repo)
        recent = repo.insert_log_signal(
            application_id=app_id, container_row_id=container_row_id, category="oom", severity="critical",
            normalized_signature="recent", first_seen_at=T3, last_seen_at=T3, count=1, sample="recent",
            source_type="container_log", source_ref="stdout+stderr",
        )
        deleted = repo.delete_expired_log_signals(before=T1)
        assert deleted == 0
        assert repo.get_log_signal(recent) is not None
        conn.close()


class TestEvidenceCollectorHeartbeat:
    def test_fresh_state_has_no_evidence_activity(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        state = repo.get_collector_state()
        assert state.last_evidence_success_at is None
        assert state.consecutive_evidence_failures == 0
        assert state.last_evidence_error is None
        conn.close()

    def test_success_resets_failure_streak(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        repo.record_evidence_tick_failure(error="boom")
        repo.record_evidence_tick_failure(error="boom again")
        repo.record_evidence_tick_success(at=T1)
        state = repo.get_collector_state()
        assert state.last_evidence_success_at == T1
        assert state.consecutive_evidence_failures == 0
        assert state.last_evidence_error is None
        conn.close()

    def test_failure_never_touches_last_success_at(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        repo.record_evidence_tick_success(at=T1)
        repo.record_evidence_tick_failure(error="oops")
        state = repo.get_collector_state()
        assert state.last_evidence_success_at == T1
        assert state.consecutive_evidence_failures == 1
        assert state.last_evidence_error == "oops"
        conn.close()

    def test_core_collector_state_is_untouched_by_evidence_methods(self, tmp_path):
        """The core tick heartbeat and the evidence heartbeat are
        independent -- an evidence failure must never look like a core
        monitoring failure."""

        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        repo.record_tick_started(at=T1)
        repo.record_tick_success(at=T1)
        repo.record_evidence_tick_failure(error="evidence broke")
        state = repo.get_collector_state()
        assert state.last_success_at == T1
        assert state.consecutive_failures == 0
        assert state.last_error is None
        assert state.consecutive_evidence_failures == 1
        conn.close()


# --------------------------------------------------------------------------
# Evidence assembler read helpers (Milestone 11)
# --------------------------------------------------------------------------


class TestGetApplicationById:
    def test_found(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app_id, _ = _seed_container(repo)
        record = repo.get_application_by_id(app_id)
        assert record.key == "cnstrct"
        conn.close()

    def test_not_found_returns_none(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        assert repo.get_application_by_id(999999) is None
        conn.close()


class TestTransitionsInWindow:
    def test_container_scope_row_carries_docker_container_id(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app_id, container_row_id = _seed_container(repo)
        repo.insert_transition(scope="container", scope_id=container_row_id, from_status=None, to_status=HealthStatus.HEALTHY, occurred_at=T1)
        rows = repo.get_transitions_in_window(app_id, window_start=T1 - timedelta(seconds=1), window_end=T1 + timedelta(seconds=1))
        assert len(rows) == 1
        assert rows[0].container_docker_id == "docker-abc"
        conn.close()

    def test_application_and_service_scope_rows_have_no_container_id(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app_id, container_row_id = _seed_container(repo)
        repo.insert_transition(scope="application", scope_id=app_id, from_status=None, to_status=HealthStatus.HEALTHY, occurred_at=T1)
        rows = repo.get_transitions_in_window(app_id, window_start=T1 - timedelta(seconds=1), window_end=T1 + timedelta(seconds=1))
        assert rows[0].container_docker_id is None
        conn.close()

    def test_upper_bound_excludes_later_transitions(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app_id, _ = _seed_container(repo)
        repo.insert_transition(scope="application", scope_id=app_id, from_status=None, to_status=HealthStatus.HEALTHY, occurred_at=T1)
        repo.insert_transition(scope="application", scope_id=app_id, from_status=HealthStatus.HEALTHY, to_status=HealthStatus.UNHEALTHY, occurred_at=T3)
        rows = repo.get_transitions_in_window(app_id, window_start=T1 - timedelta(seconds=1), window_end=T2)
        assert len(rows) == 1
        assert rows[0].to_status is HealthStatus.HEALTHY
        conn.close()

    def test_lower_bound_excludes_earlier_transitions(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app_id, _ = _seed_container(repo)
        repo.insert_transition(scope="application", scope_id=app_id, from_status=None, to_status=HealthStatus.HEALTHY, occurred_at=T1)
        repo.insert_transition(scope="application", scope_id=app_id, from_status=HealthStatus.HEALTHY, to_status=HealthStatus.UNHEALTHY, occurred_at=T3)
        rows = repo.get_transitions_in_window(app_id, window_start=T2, window_end=T3 + timedelta(seconds=1))
        assert len(rows) == 1
        assert rows[0].to_status is HealthStatus.UNHEALTHY
        conn.close()

    def test_existing_list_transitions_for_application_unaffected(self, tmp_path):
        """Backward-compat check: the pre-existing method's own shape and
        behavior (used by `argus history`) is untouched by the new
        container_docker_id column."""

        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        app_id, container_row_id = _seed_container(repo)
        repo.insert_transition(scope="container", scope_id=container_row_id, from_status=None, to_status=HealthStatus.HEALTHY, occurred_at=T1)
        rows = repo.list_transitions_for_application(app_id, since=T1 - timedelta(seconds=1))
        assert len(rows) == 1
        assert rows[0].label == "api"
        conn.close()


class TestObservationBeforeAtAfter:
    def _seed_observations(self, repo, container_row_id):
        from argus.domain.models import Container, DockerState, Observation

        def make(at, restart_count, status=HealthStatus.HEALTHY):
            container = Container(
                container_id="docker-abc", name="cnstrct-api-1", image="x", compose_project="cnstrct",
                compose_service="api", first_seen_at=T1 - timedelta(hours=1), last_seen_at=at,
            )
            return Observation(
                container_ref=container, observed_at=at, docker_state=DockerState.RUNNING, docker_health=None,
                restart_count=restart_count, exit_code=None, started_at=None, finished_at=None, ports=(),
                labels={}, derived_status=status,
            )

        for at, restart_count in ((T1, 0), (T2, 1), (T3, 2)):
            repo.insert_observation(container_row_id=container_row_id, observation=make(at, restart_count))

    def test_before_returns_the_most_recent_strictly_earlier_row(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        _, container_row_id = _seed_container(repo)
        self._seed_observations(repo, container_row_id)

        record = repo.get_observation_before("docker-abc", before=T3)
        assert record is not None
        assert record.observation.observed_at == T2
        assert isinstance(record.id, int)
        conn.close()

    def test_before_with_nothing_earlier_returns_none(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        _, container_row_id = _seed_container(repo)
        self._seed_observations(repo, container_row_id)

        assert repo.get_observation_before("docker-abc", before=T1) is None
        conn.close()

    def test_at_returns_exact_match(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        _, container_row_id = _seed_container(repo)
        self._seed_observations(repo, container_row_id)

        record = repo.get_observation_at("docker-abc", at=T2)
        assert record is not None
        assert record.observation.restart_count == 1
        conn.close()

    def test_at_with_no_exact_match_returns_none(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        _, container_row_id = _seed_container(repo)
        self._seed_observations(repo, container_row_id)

        assert repo.get_observation_at("docker-abc", at=T2 + timedelta(seconds=1)) is None
        conn.close()

    def test_after_returns_earliest_strictly_later_row(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        _, container_row_id = _seed_container(repo)
        self._seed_observations(repo, container_row_id)

        record = repo.get_observation_after("docker-abc", after=T1)
        assert record is not None
        assert record.observation.observed_at == T2
        conn.close()

    def test_after_with_nothing_later_returns_none(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        _, container_row_id = _seed_container(repo)
        self._seed_observations(repo, container_row_id)

        assert repo.get_observation_after("docker-abc", after=T3) is None
        conn.close()

    def test_ids_are_distinct_across_the_three_rows(self, tmp_path):
        conn = open_database(tmp_path / "a.db")
        repo = Repository(conn)
        _, container_row_id = _seed_container(repo)
        self._seed_observations(repo, container_row_id)

        # All three queried relative to the *same* instant (T2, the
        # middle of the three seeded rows) so they resolve to three
        # genuinely different rows: T1 (before), T2 (at), T3 (after).
        before = repo.get_observation_before("docker-abc", before=T2)
        at = repo.get_observation_at("docker-abc", at=T2)
        after = repo.get_observation_after("docker-abc", after=T2)
        assert len({before.id, at.id, after.id}) == 3
        assert (before.observation.observed_at, at.observation.observed_at, after.observation.observed_at) == (T1, T2, T3)
        conn.close()


# --------------------------------------------------------------------------
# Architecture guards
# --------------------------------------------------------------------------

FORBIDDEN_IMPORT_ROOTS = {"docker", "anthropic", "openai", "langgraph", "fastapi"}


def _imported_roots(source: str) -> set[str]:
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _called_function_names(source: str) -> set[str]:
    """Names of functions actually *called* in source -- ignores plain-text
    mentions inside docstrings/comments, unlike a substring search."""
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


class TestArchitectureGuard:
    def test_store_has_no_docker_or_ai_imports(self):
        source = inspect.getsource(database_module) + inspect.getsource(repository_module)
        found = _imported_roots(source) & FORBIDDEN_IMPORT_ROOTS
        assert not found, f"argus.store imports forbidden module(s): {found}"

    def test_store_may_import_sqlite3_and_domain(self):
        source = inspect.getsource(database_module) + inspect.getsource(repository_module)
        roots = _imported_roots(source)
        assert "sqlite3" in roots
        assert "argus" in roots

    def test_repository_does_not_import_health_module(self):
        source = inspect.getsource(repository_module)
        modules = _imported_dotted_modules(source)
        assert "argus.domain.health" not in modules
        assert not any(m.startswith("argus.domain.health") for m in modules)

    def test_repository_never_calls_health_evaluators(self):
        source = inspect.getsource(repository_module)
        called = _called_function_names(source)
        forbidden = {
            "evaluate_container_health",
            "evaluate_service_health",
            "evaluate_application_health",
        }
        assert not (called & forbidden), f"repository.py calls: {called & forbidden}"

    def test_repository_never_imports_collectors(self):
        """persist_discovery takes plain sequences, not a DiscoveryResult,
        specifically so argus.store never depends on argus.collectors."""
        source = inspect.getsource(repository_module)
        modules = _imported_dotted_modules(source)
        assert not any(m.startswith("argus.collectors") for m in modules)
