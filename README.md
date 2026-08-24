# Argus

Argus is a deterministic infrastructure monitoring substrate designed to
observe application health before AI reasoning is introduced.

> Observe reality deterministically first. Reason about it second.

Discovery, health classification, and incident detection are ordinary,
tested Python — no language model is involved anywhere in that path. A
reasoning layer is planned for a later milestone, built on top of this
substrate, not inside it.

## Current status

**Milestone 1 — Domain Model**

The `argus.domain` package defines the vocabulary every later component
depends on: `Container`, `Service`, `Application`, `Observation`,
`PortBinding`, and the `HealthStatus` / `DockerState` / `DockerHealth`
enums. Nothing else exists yet — no Docker access, no database, no CLI,
no health evaluation logic.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest
```

## API (Milestone 13)

A read-only HTTP API (`argus.api`, built on FastAPI) exposes the same
deterministic read models and persisted AI explanations the `argus` CLI
already uses — nothing new is computed here, and nothing here writes
to the database, mutates a Docker container, or calls an AI provider.
It exists so a future React dashboard can consume Argus's state without
duplicating any of that logic.

Start it locally:

```bash
argus-api
```

(equivalent to `uvicorn argus.api.app:create_app --factory --host
127.0.0.1 --port 8088`). It binds to `127.0.0.1` only, never
`0.0.0.0` — this is not exposed to the network by default. Point it at
a specific database with `ARGUS_DB_PATH` (same variable the CLI uses).

Once running:

- `http://127.0.0.1:8088/api/v1/...` — every endpoint, versioned,
  `GET`-only.
- `http://127.0.0.1:8088/docs` — interactive OpenAPI documentation.

Every `/api/v1` route is provably `GET` only (see
`tests/unit/test_api_readonly_guard.py`); the one exception to "never
touches Docker" is `GET /api/v1/system/doctor`, which calls Argus's
existing read-only `argus doctor` diagnostic subsystem and nothing
else. There is no React frontend yet — this milestone is the API layer
only.
# argus
