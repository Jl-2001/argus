-- Argus v0.1 -- Milestone 4 schema.
--
-- Only identity (applications/services/containers) and append-only
-- observation history are created here. `health_transitions`,
-- `incidents`, and `collector_state` from the approved architecture are
-- deliberately NOT created yet -- see the Milestone 4 completion report
-- for why. PRAGMA user_version (set in database.py) exists precisely so
-- those can be added later as a real, versioned schema change instead
-- of being guessed at now.
--
-- Every CREATE is idempotent (`IF NOT EXISTS`) so reopening an
-- already-initialized database never wipes or recreates its tables.

CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY,
    key             TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    is_standalone   INTEGER NOT NULL CHECK (is_standalone IN (0, 1)),
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);

-- `service_key` exists because SQLite's UNIQUE constraint treats every
-- NULL as distinct from every other NULL -- UNIQUE(application_id,
-- compose_service) would silently allow duplicate standalone-service
-- rows (compose_service IS NULL) for the same application. service_key
-- is the real compose_service value when one exists, or the fixed
-- '__standalone__' sentinel otherwise -- always non-NULL, so the
-- uniqueness constraint actually holds. compose_service itself is kept
-- as its own (nullable) column so reads reconstruct the honest domain
-- value, not the sentinel.
CREATE TABLE IF NOT EXISTS services (
    id                INTEGER PRIMARY KEY,
    application_id    INTEGER NOT NULL REFERENCES applications(id),
    compose_service   TEXT,
    service_key       TEXT NOT NULL,
    name              TEXT NOT NULL,
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    UNIQUE (application_id, service_key)
);

-- container_id is Docker's own identity string and is the permanent
-- key here -- never the container's `name`, which Docker reuses across
-- recreation.
CREATE TABLE IF NOT EXISTS containers (
    id              INTEGER PRIMARY KEY,
    service_id      INTEGER NOT NULL REFERENCES services(id),
    container_id    TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);

-- Append-only: rows here are never updated once inserted. `image` is
-- stored per-observation (not only on `containers`) to match the
-- approved v0.1 schema; it comes from Observation.container_ref.image
-- at write time. UNIQUE(container_id, observed_at) is the DB-level
-- guarantee against persisting the same logical tick twice.
CREATE TABLE IF NOT EXISTS observations (
    id               INTEGER PRIMARY KEY,
    container_id     INTEGER NOT NULL REFERENCES containers(id),
    observed_at      TEXT NOT NULL,
    docker_state     TEXT NOT NULL,
    docker_health    TEXT,
    restart_count    INTEGER NOT NULL,
    exit_code        INTEGER,
    started_at       TEXT,
    finished_at      TEXT,
    image            TEXT NOT NULL,
    ports_json       TEXT NOT NULL,
    labels_json      TEXT NOT NULL,
    derived_status   TEXT NOT NULL,
    derived_detail   TEXT,
    UNIQUE (container_id, observed_at)
);

CREATE INDEX IF NOT EXISTS ix_services_application ON services(application_id);
CREATE INDEX IF NOT EXISTS ix_containers_service ON containers(service_id);
CREATE INDEX IF NOT EXISTS ix_observations_container_time ON observations(container_id, observed_at);

-- Added in schema v2 (Milestone 5). Exactly one logical row (id = 1) --
-- the collector's own liveness, independent of anything it's watching.
-- Milestone 5 only ever writes last_tick_at/last_success_at/
-- consecutive_failures/last_error here; it never records an incident or
-- a transition -- that is Milestone 6's table, not this one.
CREATE TABLE IF NOT EXISTS collector_state (
    id                     INTEGER PRIMARY KEY CHECK (id = 1),
    last_tick_at           TEXT,
    last_success_at        TEXT,
    consecutive_failures   INTEGER NOT NULL DEFAULT 0,
    last_error             TEXT
);

-- Added in schema v3 (Milestone 6).
--
-- scope_id is a polymorphic reference (into containers/services/
-- applications depending on `scope`) rather than a real foreign key --
-- it cannot point at three different tables at once. `scope` is
-- constrained to the three known values at the DB layer as well as in
-- Python, so nothing can silently write an arbitrary scope string.
-- `observation_id` is part of the approved schema shape but is left
-- NULL by v0.1 -- see the Milestone 6 report for why.
CREATE TABLE IF NOT EXISTS health_transitions (
    id               INTEGER PRIMARY KEY,
    scope            TEXT NOT NULL CHECK (scope IN ('container', 'service', 'application')),
    scope_id         INTEGER NOT NULL,
    from_status      TEXT,
    to_status        TEXT NOT NULL,
    occurred_at      TEXT NOT NULL,
    observation_id   INTEGER REFERENCES observations(id)
);
CREATE INDEX IF NOT EXISTS ix_transitions_scope ON health_transitions(scope, scope_id, occurred_at);

-- v0.1 opens incidents at application scope only -- the CHECK enforces
-- that at the DB layer too; finer-grained container/service incidents
-- are a later milestone's extension, not implemented here. The partial
-- unique index is the DB-level half of incident deduplication: at most
-- one *open* row per failure_signature, enforced even if application
-- logic is ever bypassed or buggy.
CREATE TABLE IF NOT EXISTS incidents (
    id                       INTEGER PRIMARY KEY,
    scope                    TEXT NOT NULL DEFAULT 'application' CHECK (scope = 'application'),
    scope_id                 INTEGER NOT NULL REFERENCES applications(id),
    failure_signature        TEXT NOT NULL,
    opened_at                TEXT NOT NULL,
    closed_at                TEXT,
    status                   TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
    opening_status           TEXT NOT NULL,
    worst_status             TEXT NOT NULL,
    opening_transition_id    INTEGER NOT NULL REFERENCES health_transitions(id),
    resolving_transition_id  INTEGER REFERENCES health_transitions(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_incidents_open_signature ON incidents(failure_signature) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS ix_incidents_scope ON incidents(scope, scope_id);
