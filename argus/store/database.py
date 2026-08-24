"""Connection lifecycle and schema bootstrap for the Argus SQLite store.

Deliberately separate from ``repository.py``: this module only knows
how to open a connection, enable the required PRAGMAs, and make sure
the schema exists. It has no idea what an Application or an
Observation is, and it never decides anything about health.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_DB_PATH",
    "REQUIRED_TABLES",
    "PersistenceError",
    "DatabaseOpenError",
    "SchemaError",
    "DuplicateObservationError",
    "DuplicateIncidentError",
    "DuplicateExplanationError",
    "open_database",
    "open_database_readonly",
    "initialize_database",
    "default_database_path",
    "DatabaseInspection",
    "inspect_database_readonly",
]

SCHEMA_VERSION = 6

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

#: Where Argus keeps its database when nothing more specific is given.
#: One shared default, used identically by the collector entrypoint and
#: the CLI -- see `default_database_path`.
DEFAULT_DB_PATH = "./data/argus.db"

#: Every table a healthy Argus database must have by SCHEMA_VERSION.
#: Used only by `inspect_database_readonly` (Milestone 8's `argus doctor`
#: Database check) -- normal reads never need to enumerate this.
REQUIRED_TABLES: tuple[str, ...] = (
    "applications",
    "services",
    "containers",
    "observations",
    "collector_state",
    "health_transitions",
    "incidents",
    "log_cursors",
    "log_signals",
    "incident_evidence",
    "incident_explanations",
)


class PersistenceError(RuntimeError):
    """Base class for every error this package raises.

    Callers see this (or a subclass), never a raw ``sqlite3`` exception
    -- but the original is always chained via ``from exc`` so the real
    cause is still visible for debugging.
    """


class DatabaseOpenError(PersistenceError):
    """The database file/connection could not be opened at all."""


class SchemaError(PersistenceError):
    """The database's schema version is incompatible with this build of Argus."""


class DuplicateObservationError(PersistenceError):
    """An observation for this (container, observed_at) pair already exists."""


class DuplicateIncidentError(PersistenceError):
    """A second *open* incident was attempted for a failure_signature that
    already has one. Application-level dedup logic should always prevent
    this from being reached in practice -- this is the DB-level backstop
    (the schema's partial unique index) firing, not the normal path."""


class DuplicateExplanationError(PersistenceError):
    """A second explanation was attempted for the exact same (incident,
    bundle_fingerprint, model, prompt_version) key. Milestone 12's own
    cache-lookup-before-call logic should always prevent this in
    practice -- this is the DB-level backstop (the schema's own UNIQUE
    constraint) firing, not the normal path."""


def default_database_path(
    explicit: str | Path | None = None, *, create_parent: bool = True
) -> Path:
    """Resolve the one Argus database path, the same way everywhere.

    Precedence: ``explicit`` (e.g. a CLI ``--database`` flag) > the
    ``ARGUS_DB_PATH`` environment variable > `DEFAULT_DB_PATH`. Shared by
    ``argus.run_collector`` and ``argus.cli`` so there is exactly one
    place this decision is made, not two competing config mechanisms.

    Creates the containing directory if needed (private to this user,
    ``0700`` -- this is local, potentially sensitive operational data),
    unless ``create_parent=False``. Every existing caller (the collector
    entrypoint, every ordinary CLI command) wants the directory created,
    since they go on to call `open_database` next, which itself
    bootstraps the file. `argus doctor` is the one caller that passes
    ``create_parent=False``: it must resolve the same path without the
    side effect of creating anything, since diagnosing "does this exist"
    is exactly what it's trying to answer.
    """

    raw = explicit if explicit is not None else os.environ.get("ARGUS_DB_PATH", DEFAULT_DB_PATH)
    path = Path(raw)
    if create_parent:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def open_database(path: str | Path) -> sqlite3.Connection:
    """Open (creating if necessary) the Argus SQLite database at ``path``.

    Enables foreign keys and WAL journaling, then ensures the schema
    exists. Safe to call repeatedly against the same file: schema
    creation is idempotent (``CREATE TABLE IF NOT EXISTS``), so
    reopening an already-initialized database never wipes or recreates
    its tables.
    """

    try:
        connection = sqlite3.connect(str(path))
    except sqlite3.Error as exc:
        raise DatabaseOpenError(f"could not open database at {path!r}: {exc}") from exc

    # Full autocommit: each individual statement commits immediately unless
    # it's inside an explicit BEGIN...COMMIT/ROLLBACK (which
    # repository.Repository.persist_discovery uses for its one-snapshot
    # transaction). With sqlite3's *default* isolation_level (""), Python
    # instead starts an implicit transaction before every write and never
    # commits it on its own -- closing the connection would silently
    # discard anything a caller forgot to commit by hand. None avoids that
    # footgun for the repository's individual, standalone write methods.
    connection.isolation_level = None

    connection.row_factory = sqlite3.Row

    # Both PRAGMAs must run before any transaction is opened -- in
    # particular, SQLite silently ignores `foreign_keys` if set while a
    # transaction is already active. A freshly opened connection has none.
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")

    initialize_database(connection)
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    """Create the schema if it doesn't exist yet, and check/apply the schema version.

    ``schema.sql`` is written to be idempotent (``CREATE TABLE/INDEX IF
    NOT EXISTS``) and already contains every table through
    ``SCHEMA_VERSION`` -- running it is always safe, whether this is a
    brand new database or an existing one being opened for migration,
    and it never drops or rewrites an existing table.

    What ``schema.sql`` alone does *not* do is decide whether a
    pre-existing database's ``user_version`` is one this build actually
    understands, or move it forward explicitly. That is this
    function's job:

    * ``user_version == 0`` -- a brand new database. Stamped straight
      to ``SCHEMA_VERSION`` (the just-executed script already created
      every table).
    * ``user_version == 1`` -- a genuine Milestone 4 database. Migrated
      forward one explicit step at a time (currently just v1 -> v2);
      existing identity/observation rows are untouched by this, since
      the migration only adds the new ``collector_state`` table.
    * ``user_version == SCHEMA_VERSION`` already -- left alone.
    * anything else (including a version from some future build this
      one doesn't know how to read) -- raises ``SchemaError`` rather
      than silently downgrading or guessing.
    """

    schema_sql = _SCHEMA_PATH.read_text()
    try:
        connection.executescript(schema_sql)
    except sqlite3.Error as exc:
        raise SchemaError(f"could not initialize schema: {exc}") from exc

    current_version = connection.execute("PRAGMA user_version").fetchone()[0]

    if current_version == 0:
        # A brand-new database: the script just above already created
        # `incident_explanations` with `provider` as a real column, so
        # its indexes are safe to create right now.
        _create_incident_explanations_indexes(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return

    for migration in _MIGRATIONS:
        if current_version == migration.from_version:
            migration.apply(connection)
            current_version = migration.to_version

    if current_version != SCHEMA_VERSION:
        raise SchemaError(
            f"database schema version {current_version} is not supported by this build "
            f"of Argus (expected {SCHEMA_VERSION}); migrations are not implemented beyond it"
        )


@dataclass(frozen=True, slots=True)
class _Migration:
    from_version: int
    to_version: int
    apply: "Callable[[sqlite3.Connection], None]"


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """v1 -> v2 (Milestone 5): adds ``collector_state``.

    The table itself was already created by the shared, idempotent
    ``schema.sql`` script run just before this -- that script contains
    every table through the current version regardless of the
    database's starting version, specifically so an ``ALTER``-free "add
    a table" migration like this one doesn't need its own separate DDL.
    This function's real job is the explicit, auditable version bump: a
    v1 database is not silently treated as though it had always been
    v2, and nothing here touches the pre-existing applications/
    services/containers/observations rows.
    """

    connection.execute("PRAGMA user_version = 2")


def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
    """v2 -> v3 (Milestone 6): adds ``health_transitions`` and ``incidents``.

    Same shape as the v1 -> v2 migration: both tables already exist by
    the time this runs (created by the shared schema script), so this
    is purely the explicit version bump -- nothing here touches any
    pre-existing row in any table.
    """

    connection.execute("PRAGMA user_version = 3")


def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
    """v3 -> v4 (Milestone 10): adds the evidence tables
    (``log_cursors``/``log_signals``/``incident_evidence``, already
    created by the shared schema script) and three new columns on the
    *pre-existing* ``collector_state`` row -- unlike every migration
    before it, this one genuinely needs an ``ALTER TABLE``, since
    ``collector_state`` already exists on a real v3 database with a
    fixed, narrower column set that ``CREATE TABLE IF NOT EXISTS`` alone
    cannot widen.

    Guarded by ``PRAGMA table_info`` so this is safe to run even if
    somehow invoked twice -- consistent with every other part of this
    module treating schema evolution as idempotent, not "runs exactly
    once and hopes".
    """

    existing_columns = {row["name"] for row in connection.execute("PRAGMA table_info(collector_state)")}
    for column, ddl in (
        ("last_evidence_success_at", "ALTER TABLE collector_state ADD COLUMN last_evidence_success_at TEXT"),
        (
            "consecutive_evidence_failures",
            "ALTER TABLE collector_state ADD COLUMN consecutive_evidence_failures INTEGER NOT NULL DEFAULT 0",
        ),
        ("last_evidence_error", "ALTER TABLE collector_state ADD COLUMN last_evidence_error TEXT"),
    ):
        if column not in existing_columns:
            connection.execute(ddl)

    connection.execute("PRAGMA user_version = 4")


def _migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
    """v4 -> v5 (Milestone 12): adds ``incident_explanations`` (already
    created by the shared schema script). Purely additive -- no
    existing table, column, or row is touched. Every v0.1/v0.2 evidence
    table (observations, health_transitions, incidents, log_signals,
    incident_evidence, collector_state) is preserved exactly as-is.
    """

    connection.execute("PRAGMA user_version = 5")


def _migrate_v5_to_v6(connection: sqlite3.Connection) -> None:
    """v5 -> v6 (Milestone 12.1): adds ``incident_explanations.provider``
    and widens the explanation cache key to include it.

    A genuine v5 database's ``incident_explanations`` table predates
    ``provider`` entirely and still carries the *old* inline
    ``UNIQUE(incident_id, bundle_fingerprint, model, prompt_version)``
    constraint. SQLite backs an inline constraint like that with an
    unnamed index that is part of the table definition itself -- unlike
    every earlier migration in this file (which only ever needed to
    *add* a column or a whole new table), there is no ``ALTER
    TABLE``/``DROP INDEX`` that can widen or remove it; SQLite itself
    refuses (``index associated with UNIQUE or PRIMARY KEY constraint
    cannot be dropped``). The only correct fix is SQLite's own standard
    table-rebuild procedure: rename the old table aside, let the schema
    script (already loaded once for this call) recreate
    ``incident_explanations`` under its real name with the new shape,
    copy every row across by explicit column name (so a
    forgotten/reordered column can never silently misalign), then drop
    the renamed-aside copy.

    Backfilling ``provider = 'anthropic'`` for every pre-existing row is
    a statement of historical fact, not a guess -- Gemini support did
    not exist before Milestone 12.1, so every explanation persisted
    before now was, in fact, generated by Anthropic.
    """

    existing_columns = {row["name"] for row in connection.execute("PRAGMA table_info(incident_explanations)")}
    if "provider" in existing_columns:
        connection.execute("PRAGMA user_version = 6")
        return

    connection.execute("ALTER TABLE incident_explanations RENAME TO incident_explanations_v5")
    # Recreates `incident_explanations` under its real name, in the new
    # (v6) shape -- every other statement in the script is a no-op here
    # (everything else already exists); this is the one table that
    # doesn't, since it was just renamed away.
    connection.executescript(_SCHEMA_PATH.read_text())
    connection.execute(
        "INSERT INTO incident_explanations "
        "(id, incident_id, bundle_fingerprint, provider, model, prompt_version, created_at, "
        " summary, root_cause, confidence, input_tokens, output_tokens, response_json) "
        "SELECT id, incident_id, bundle_fingerprint, 'anthropic', model, prompt_version, created_at, "
        "       summary, root_cause, confidence, input_tokens, output_tokens, response_json "
        "FROM incident_explanations_v5"
    )
    connection.execute("DROP TABLE incident_explanations_v5")

    _create_incident_explanations_indexes(connection)
    connection.execute("PRAGMA user_version = 6")


def _create_incident_explanations_indexes(connection: sqlite3.Connection) -> None:
    """Creates the two `incident_explanations` indexes that reference
    `provider` -- split out of `schema.sql`'s own unconditional script
    because that script must stay safe to run against a database at any
    prior version, and a genuine pre-Milestone-12.1 table doesn't have
    this column at the point the script runs (see schema.sql's own
    comment). Called once for a brand-new database and once from
    `_migrate_v5_to_v6`, each only after `provider` is guaranteed to
    exist. Idempotent (`IF NOT EXISTS`), like every other index in this
    codebase.
    """

    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_incident_explanations_cache_key "
        "ON incident_explanations(incident_id, bundle_fingerprint, provider, model, prompt_version)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_incident_explanations_lookup "
        "ON incident_explanations(incident_id, bundle_fingerprint, provider, model, prompt_version)"
    )


# Ordered so a database more than one version behind could, in principle,
# walk forward one step at a time.
_MIGRATIONS: tuple[_Migration, ...] = (
    _Migration(from_version=1, to_version=2, apply=_migrate_v1_to_v2),
    _Migration(from_version=2, to_version=3, apply=_migrate_v2_to_v3),
    _Migration(from_version=3, to_version=4, apply=_migrate_v3_to_v4),
    _Migration(from_version=4, to_version=5, apply=_migrate_v4_to_v5),
    _Migration(from_version=5, to_version=6, apply=_migrate_v5_to_v6),
)


def open_database_readonly(path: str | Path) -> sqlite3.Connection:
    """A genuinely read-only connection, for diagnostics.

    Unlike `open_database`, this never creates the file, never runs
    `initialize_database`, and never migrates anything -- SQLite's own
    ``mode=ro`` URI flag means any write attempt against the returned
    connection fails at the SQLite layer itself, not merely by
    convention. This is what makes it safe for `argus doctor` to use:
    doctor diagnoses whether a database is usable, it must never be the
    reason one gets created or changed. Raises `sqlite3.Error` directly
    (not wrapped) -- callers decide how to turn "couldn't open" into a
    diagnostic result.
    """

    connection = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


@dataclass(frozen=True, slots=True)
class DatabaseInspection:
    """The raw facts `inspect_database_readonly` found -- no judgment
    about whether they add up to a healthy database. Deciding that
    ("Database: PASS/FAIL") is `argus.doctor.checks`'s job, not this
    module's -- consistent with the rest of `argus.store` never
    deciding anything beyond persistence mechanics.
    """

    exists: bool
    opened: bool
    schema_version: Optional[int]
    missing_tables: tuple[str, ...]
    error: Optional[str]


def inspect_database_readonly(path: str | Path) -> DatabaseInspection:
    """Read-only diagnostic inspection of a database file for `argus
    doctor`'s Database check -- never creates, initializes, or migrates
    anything, in deliberate contrast to `open_database`, which is
    allowed to bootstrap a fresh database for every other command's
    normal use. A missing file is reported as a fact (``exists=False``),
    never silently created.
    """

    path = Path(path)
    if not path.exists():
        return DatabaseInspection(
            exists=False, opened=False, schema_version=None,
            missing_tables=REQUIRED_TABLES, error="database file does not exist",
        )

    try:
        connection = open_database_readonly(path)
    except sqlite3.Error as exc:
        return DatabaseInspection(
            exists=True, opened=False, schema_version=None,
            missing_tables=REQUIRED_TABLES, error=f"could not open database: {exc}",
        )

    try:
        try:
            schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
            table_rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            # e.g. "file is not a database" -- a real file that isn't SQLite at all
            return DatabaseInspection(
                exists=True, opened=False, schema_version=None,
                missing_tables=REQUIRED_TABLES, error=f"database file is malformed: {exc}",
            )
    finally:
        connection.close()

    present_tables = {row["name"] for row in table_rows}
    missing = tuple(table for table in REQUIRED_TABLES if table not in present_tables)

    return DatabaseInspection(
        exists=True, opened=True, schema_version=schema_version, missing_tables=missing, error=None,
    )
