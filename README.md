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
else.

## Dashboard (Milestone 14)

A read-only React dashboard lives in `web/` (Vite + TypeScript +
Tailwind + TanStack Query) — see `web/README.md` for the two-terminal
local startup (`argus-api` + `npm run dev`). It's the first Argus
frontend; there's no voice and no actions
(start/stop/restart/repair/generate) yet.

## Real-time dashboard (Milestone 15)

`GET /api/v1/events` streams Server-Sent Events (SSE) — a
**server → browser only** channel, deliberately not WebSockets: nothing
in Argus needs the browser to push anything back to the server in
real time (see `argus/api/routes/events.py`'s own docstring). Each
message means "something changed" (an application's status, an
incident opening/escalating/resolving, new evidence, a persisted AI
explanation) — it is never the authoritative state itself. The
dashboard responds by invalidating the relevant cached query
(`web/src/realtime/invalidation.ts`) and re-fetching the real answer
from the existing `GET` endpoints.

Polling remains the fallback, not a redundant mechanism SSE replaces —
every page still refetches on its own interval (30s–60s) even if the
SSE connection drops, so a lost/reconnecting connection degrades to
"a little less instant," never "broken." The top bar shows the SSE
connection's own state (`Live` / `Reconnecting…` /
`Realtime disconnected — polling`), kept visually distinct from
Argus's collector health, which is a different signal entirely.

No command or control ever flows through SSE — it is exactly as
read-only as the rest of `/api/v1`; see
`tests/unit/test_api_readonly_guard.py`.

## Multi-host monitoring (Milestone 16)

A single Argus installation can now monitor more than one machine.

**Single-host mode** (unchanged): `python -m argus.run_collector` +
`argus-api` + `web/` on one machine, exactly as every earlier milestone
describes above. No agent, no extra configuration, nothing new to
learn — every existing database upgrades in place (see "Schema
migration" below).

**Multi-host mode**: the *control plane* (the machine running
`argus-api`/the dashboard — your existing single-host setup) plus one
small `argus-agent` process per additional machine you want monitored:

```
                 ARGUS CONTROL PLANE
                      (e.g. MacBook)
                         |
             +-----------+-----------+
             | React + FastAPI       |
             | persistence           |
             | incidents/evidence    |
             | AI explanation        |
             +-----------+-----------+
                         |
                 authenticated HTTPS
                  / private network
                         |
              +----------+----------+
              v                     v
        local collector       argus-agent
      (this machine's own      (e.g. Ubuntu Dell)
       Docker socket)                |
                                      v
                                its own Docker socket
```

The control plane never talks to a remote Docker socket, directly or
indirectly — `argus-agent` is the *only* process that ever touches the
Docker daemon on the machine it runs on, and it only ever reads from
it (list/inspect/logs — see `argus.collectors.docker_client`, which
`argus-agent` reuses unchanged). It periodically collects a small,
already-sanitized snapshot (application/container facts, never raw
env vars, mount paths, Docker labels, or the Docker socket itself) and
**POSTs** it to the control plane's `POST /api/v1/agents/ingest` — the
one deliberate, narrowly-scoped exception to every other `/api/v1`
route being `GET`-only (see `argus/api/routes/agents.py`). The agent
never opens an inbound port and never receives a connection from the
control plane.

> **Never expose the Docker socket or an unauthenticated Docker TCP
> API to the network.** `argus-agent` reads Docker locally, over the
> same Unix socket the local collector already uses — nothing about
> this milestone requires (or should ever be made to require) opening
> `tcp://` access to a Docker daemon.

Register a remote host once, from the control plane:

```bash
argus agents add dell-latitude-5400 --name "Ubuntu Dell"
```

This prints an `ARGUS_AGENT_TOKEN` exactly once — set it (with
`ARGUS_AGENT_ID`/`ARGUS_HOST_KEY`/`ARGUS_CONTROL_PLANE_URL`) as
environment variables on the remote machine, then run:

```bash
argus-agent
```

`argus agents` / `argus agents inspect <host>` (and the read-only
`GET /api/v1/hosts` / `/{host_key}`, and the dashboard's **Hosts**
page) show every registered host's connectivity (`ONLINE`/`STALE`/
`OFFLINE`) and application count. Applications from a remote host
appear in the existing Applications/Overview pages with their host
name attached — no separate remote-only view, no remote-specific SSE
event handling; the same `GET` endpoints and the same Milestone 15
realtime events cover both.

No remote-control surface exists or is planned as part of this
milestone: `argus-agent` cannot start/stop/restart/exec/mutate
anything, on any host, ever. See `argus/agent/`'s own docstrings for
the enforced import/mutation boundary.

### Schema migration

A pre-Milestone-16 database opens exactly as before — every existing
application/container row is backfilled onto one synthetic `local`
host (see `argus.store.database._migrate_v7_to_v8`), and every
existing application `key` is completely unchanged (a local host's
keys are never prefixed). Nothing is orphaned, nothing needs to be
re-discovered.

### Persistent multi-host deployment

Multi-host monitoring, cross-host SSE, persistent services (macOS
`launchd` for the control plane, `systemd --user` for a remote host),
and reboot validation on both machines have all been run and
confirmed working end to end, over an SSH reverse tunnel — see
`docs/multi-host-deployment.md` for the full architecture, install
steps, validation commands, and the specific issues found (Docker
context drift, Docker Desktop's startup timing, the remote
dashboard's API URL) and how they were resolved. Sanitized service
templates live in `deploy/macos/` and `deploy/linux/`.

| | Status |
|---|---|
| Multi-host monitoring | DONE |
| Cross-host SSE | DONE |
| Persistent services | DONE |
| Reboot validation | DONE |

Next up: richer host/topology views on top of the existing Hosts page
and `ApplicationTopology` (see `web/src/pages/HostsPage.tsx` and
`web/src/components/topology/ApplicationTopology.tsx`'s own docstring,
"the first, deliberately literal Argus topology view").
# argus
