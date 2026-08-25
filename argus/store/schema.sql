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

-- Added in schema v8 (Milestone 16 -- Secure Multi-Host Agent
-- Architecture). One row per monitored *machine*, not per Docker
-- object -- a `Host` is what `argus.domain.host` and this whole
-- milestone are about: which physical/virtual machine a fact came
-- from. `host_key` is a stable, human-chosen identifier ("dell-
-- latitude-5400"), never an IP address (see the milestone's own "Do
-- not use IP addresses as permanent identity"). `kind` distinguishes
-- the one synthetic `'local'` row every database has (see
-- argus.domain.host.LOCAL_HOST_KEY) from a real remote `'agent'` host.
-- `agent_token_hash` is NULL for the local host (it authenticates
-- nothing -- the local collector writes directly, in-process) and,
-- for an agent host, is always a hash (see argus.security.hash_token)
-- -- this column never holds a plaintext credential. `last_seen_at` is
-- this host's own heartbeat, updated on every core tick (local) or
-- every successfully-authenticated ingest (agent); host ONLINE/STALE/
-- OFFLINE status is *computed* from it at read time
-- (argus.domain.host.evaluate_host_status), never stored, so it can
-- never go stale itself.
-- `agent_id` (distinct from `host_key`) is the credential identity a
-- bearer token is actually issued/verified against -- `host_key` is
-- the stable, human-chosen *display*/grouping identity a snapshot
-- separately *claims* to be reporting for. Keeping the two separate is
-- what makes "Known agent but wrong host identity: HTTP 403" (the
-- milestone's own requirement) a real, checkable condition rather than
-- a tautology: `argus.api.routes.agents` looks a host up by the
-- request's `agent_id`, verifies the token against *that* row, and
-- only *then* checks whether the request's own `host_key` actually
-- matches that row's `host_key` -- a valid token whose snapshot claims
-- a different host is rejected, not silently trusted. NULL for the
-- local host (it authenticates nothing).
CREATE TABLE IF NOT EXISTS hosts (
    id                  INTEGER PRIMARY KEY,
    host_key            TEXT NOT NULL UNIQUE,
    agent_id            TEXT UNIQUE,
    display_name        TEXT NOT NULL,
    kind                TEXT NOT NULL CHECK (kind IN ('local', 'agent')),
    agent_token_hash    TEXT,
    agent_version       TEXT,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL
);

-- `host_id` was added in schema v8 (Milestone 16). `key` stays a single,
-- globally-unique column on purpose -- see argus.domain.host's own
-- `scope_application_key`: a non-local host's local key is prefixed
-- with its host_key (e.g. "dell:cnstrct") *before* it ever reaches
-- this table, so uniqueness is achieved by the value itself, not by
-- widening this constraint to UNIQUE(host_id, key). That is what lets
-- this column stay untouched by the v8 migration (a plain, additive
-- `ALTER TABLE ... ADD COLUMN`) instead of the rename/rebuild dance
-- `_migrate_v5_to_v6` needed for a genuine constraint change.
CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY,
    key             TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    is_standalone   INTEGER NOT NULL CHECK (is_standalone IN (0, 1)),
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    host_id         INTEGER REFERENCES hosts(id)
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
-- `host_id` was added in schema v8 (Milestone 16) -- denormalized from
-- the owning application/service purely so a container row carries its
-- own host without a join, matching the milestone's own "container
-- identity must effectively include host_id + docker_container_id"
-- requirement. `container_id` itself stays globally UNIQUE rather than
-- UNIQUE(host_id, container_id): Docker assigns it as a 64-hex-
-- character cryptographically-random string, so a real cross-host
-- collision is not a practical concern (the same guarantee tools like
-- Portainer already rely on) -- widening this constraint would need
-- the same rename/rebuild migration `_migrate_v5_to_v6` used for
-- `incident_explanations`, for a risk this milestone judged not worth
-- it. Documented explicitly, not silently assumed.
CREATE TABLE IF NOT EXISTS containers (
    id              INTEGER PRIMARY KEY,
    service_id      INTEGER NOT NULL REFERENCES services(id),
    container_id    TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    host_id         INTEGER REFERENCES hosts(id)
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
-- ix_applications_host / ix_containers_host are deliberately NOT created
-- here, unlike every other index above -- this script runs
-- unconditionally against a database at *any* prior schema version
-- (see this file's own opening comment), and a genuine pre-Milestone-16
-- database's applications/containers tables don't have a `host_id`
-- column yet at the point this script runs (schema.sql always runs
-- before migrations). See `database._create_host_indexes`, called once
-- for a brand-new database and once from `_migrate_v7_to_v8`, each only
-- after `host_id` is guaranteed to exist -- same pattern as
-- `_create_incident_explanations_indexes` below for `provider`.

-- Added in schema v2 (Milestone 5). Exactly one logical row (id = 1) --
-- the collector's own liveness, independent of anything it's watching.
-- Milestone 5 only ever writes last_tick_at/last_success_at/
-- consecutive_failures/last_error here; it never records an incident or
-- a transition -- that is Milestone 6's table, not this one.
--
-- The three evidence_* columns were added in schema v4 (Milestone 10)
-- -- see database.py's `_migrate_v3_to_v4` for the ALTER TABLE that
-- adds them to a pre-existing v3 database (a fresh database gets them
-- straight from this CREATE TABLE). They record the *evidence*
-- subsystem's own liveness, deliberately separate from
-- last_tick_at/last_success_at/consecutive_failures/last_error above,
-- which remain about core discovery/health monitoring only -- an
-- evidence-collection failure must never look like a core monitoring
-- failure (see argus.collector.loop's own report on this).
CREATE TABLE IF NOT EXISTS collector_state (
    id                              INTEGER PRIMARY KEY CHECK (id = 1),
    last_tick_at                    TEXT,
    last_success_at                 TEXT,
    consecutive_failures            INTEGER NOT NULL DEFAULT 0,
    last_error                      TEXT,
    last_evidence_success_at        TEXT,
    consecutive_evidence_failures   INTEGER NOT NULL DEFAULT 0,
    last_evidence_error             TEXT
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

-- Added in schema v4 (Milestone 10).
--
-- One row per per-container log-reading cursor -- "the timestamp of the
-- last log line Argus has already read for this container", so a
-- restarted process (or the next tick) never re-ingests the same lines
-- twice. Deliberately its own table, not a column on `containers`: it
-- is purely evidence-collection bookkeeping, not part of a container's
-- identity, and keeping it separate means a schema mistake here can
-- never corrupt the identity table Milestones 1-9 already depend on.
CREATE TABLE IF NOT EXISTS log_cursors (
    container_id     INTEGER PRIMARY KEY REFERENCES containers(id),
    last_log_at      TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- One row per *aggregated* evidence signal (see
-- argus.evidence.aggregator) -- never one row per raw log line.
-- `sample` is always already-redacted text (see argus.evidence.redaction)
-- -- no code path in this codebase ever writes an unredacted sample
-- here. `source_type`/`source_ref` distinguish a log-derived signal
-- ("container_log"/"stdout" or "stderr") from a Docker-fact-derived one
-- ("docker_fact"/"restart_count" or "docker_health") -- see
-- argus.domain.models.EvidenceRecord and argus.evidence.collector.
--
-- Indexes: `ix_log_signals_key` supports the aggregator's own
-- find-most-recent-matching-bucket lookup (container, category,
-- signature); `ix_log_signals_application_time` supports both the CLI's
-- `--since` filtering and incident association's window-overlap query,
-- both of which filter by application and time range.
--
-- Retention: unlinked signals (never referenced by `incident_evidence`)
-- are eligible for deletion after a bounded window (see
-- `Repository.delete_expired_log_signals`); a signal linked to any
-- incident is retained indefinitely, matching an incident's own history
-- being permanent -- see the Milestone 10 report for the full retention
-- policy.
CREATE TABLE IF NOT EXISTS log_signals (
    id                     INTEGER PRIMARY KEY,
    application_id         INTEGER NOT NULL REFERENCES applications(id),
    container_id           INTEGER NOT NULL REFERENCES containers(id),
    category               TEXT NOT NULL,
    severity               TEXT NOT NULL,
    normalized_signature   TEXT NOT NULL,
    first_seen_at          TEXT NOT NULL,
    last_seen_at           TEXT NOT NULL,
    count                  INTEGER NOT NULL DEFAULT 1,
    sample                 TEXT NOT NULL,
    source_type            TEXT NOT NULL CHECK (source_type IN ('container_log', 'docker_fact')),
    source_ref             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_log_signals_key
    ON log_signals(container_id, category, normalized_signature, last_seen_at);
CREATE INDEX IF NOT EXISTS ix_log_signals_application_time ON log_signals(application_id, first_seen_at);

-- Links evidence to an incident by time proximity only. `relation` is
-- CHECK-constrained to a single explicit value: this table records
-- "this evidence occurred near this incident's window", never
-- "this evidence caused this incident" -- there is deliberately no
-- causal value it could ever hold in v0.2. UNIQUE(incident_id,
-- log_signal_id) makes linking idempotent -- re-running association
-- for an already-linked signal is a no-op, not a duplicate row.
CREATE TABLE IF NOT EXISTS incident_evidence (
    id              INTEGER PRIMARY KEY,
    incident_id     INTEGER NOT NULL REFERENCES incidents(id),
    log_signal_id   INTEGER NOT NULL REFERENCES log_signals(id),
    linked_at       TEXT NOT NULL,
    relation        TEXT NOT NULL DEFAULT 'temporal_proximity' CHECK (relation = 'temporal_proximity'),
    UNIQUE (incident_id, log_signal_id)
);
CREATE INDEX IF NOT EXISTS ix_incident_evidence_incident ON incident_evidence(incident_id);

-- Added in schema v5 (Milestone 12).
--
-- One row per validated, trusted Claude explanation. Never stores an
-- API key, a raw unredacted log, or the system prompt itself -- only
-- what's needed to audit *which* incident, *which* evidence
-- (bundle_fingerprint), *which* model/prompt version, and *what*
-- validated response resulted. `response_json` is the full serialized
-- `argus.ai.models.IncidentExplanation` (already validated before ever
-- reaching this table -- see `argus.ai.validation`); `summary`/
-- `root_cause`/`confidence` are denormalized copies of fields already
-- inside `response_json`, kept as their own columns purely so a human
-- (or a future query) can see them without parsing JSON.
--
-- UNIQUE(incident_id, bundle_fingerprint, model, prompt_version) *is*
-- the cache: the same incident, evidence content, model, and prompt
-- version never gets a second row -- a genuinely new fingerprint
-- (evidence changed) or a new model/prompt version does. Nothing here
-- ever overwrites a prior explanation; history is append-only, same
-- discipline as `observations`/`health_transitions`.
--
-- `provider` was added in schema v6 (Milestone 12.1 -- multi-provider
-- AI). Deliberately NOT part of an inline table-level UNIQUE constraint
-- (see `ux_incident_explanations_cache_key` below instead) -- an inline
-- constraint becomes an unnamed, undroppable SQLite auto-index, which
-- would have made *this exact* migration (widening the cache key to
-- include provider) impossible without rebuilding the whole table. A
-- separate, explicitly-named unique index can always be dropped and
-- recreated with a different column list later, so this schema is
-- deliberately built that way from the start.
CREATE TABLE IF NOT EXISTS incident_explanations (
    id                   INTEGER PRIMARY KEY,
    incident_id          INTEGER NOT NULL REFERENCES incidents(id),
    bundle_fingerprint   TEXT NOT NULL,
    provider             TEXT NOT NULL DEFAULT 'anthropic',
    model                TEXT NOT NULL,
    prompt_version       TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    summary              TEXT NOT NULL,
    root_cause           TEXT,
    confidence           TEXT NOT NULL,
    input_tokens         INTEGER,
    output_tokens        INTEGER,
    response_json        TEXT NOT NULL
);
-- The cache key: (incident, evidence content, provider, model, prompt
-- version). A Gemini explanation and a Claude explanation for the same
-- incident/fingerprint are different rows -- neither overwrites the
-- other.
--
-- Deliberately NOT created here: this script must stay safe to run
-- unconditionally against a database at *any* prior schema version
-- (that is its whole contract -- see database.py's own docstring), and
-- a genuine pre-Milestone-12.1 database's `incident_explanations` table
-- doesn't have a `provider` column yet at the point this script runs
-- (schema.sql always runs before migrations) -- an index referencing it
-- here would fail against exactly that database. See
-- `database._create_incident_explanations_indexes`, called once for a
-- brand-new database and once from the `_migrate_v5_to_v6` migration,
-- each at the point `provider` is actually guaranteed to exist.

-- Milestone 15 -- the realtime event log GET /api/v1/events streams
-- from. `id` is the monotonic sequence number SSE's own `id:` field and
-- `Last-Event-ID` replay are built on (see argus.realtime); it is NOT a
-- second source of truth for application/incident/evidence state --
-- every row here only ever says "something changed, go re-fetch the
-- authoritative GET endpoint", never carries the state itself beyond a
-- few bounded identifying fields. `payload_json` is a small, sanitized
-- object (ids/keys/statuses/timestamps/counts only -- see
-- argus.realtime.emitter) -- never a raw log sample, an env var, a
-- Docker label, or anything from an AI explanation's own text.
-- Cross-process safe by construction: whichever process (the collector
-- loop, `argus explain`, argus-api itself) commits the underlying
-- domain write also inserts the matching row here, in the same SQLite
-- file every process already shares -- the API process's SSE endpoint
-- only ever reads this table, on a short poll, regardless of which
-- process produced a given row.
CREATE TABLE IF NOT EXISTS realtime_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type      TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_realtime_events_occurred_at ON realtime_events(occurred_at);
