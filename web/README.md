# Argus Dashboard (`web/`)

Milestone 14's React frontend (real-time updates added in Milestone
15; multi-host awareness added in Milestone 16): a read-only
operations dashboard over the Argus FastAPI read API (`argus.api`,
Milestone 13). It never talks to SQLite or Docker directly, never
sends a non-`GET` request, and never triggers AI generation -- see
`src/api/client.ts`'s own docstring and
`src/tests/api/readOnlyGuard.test.ts` for the enforced guarantee. That
guarantee covers `src/realtime/` too: `EventSource` is a browser-native
read-only subscription, not a request this app's own code issues.

A **Hosts** page (`src/pages/HostsPage.tsx`) lists every registered
monitored host (local plus any remote `argus-agent`) with its own
`ONLINE`/`STALE`/`OFFLINE` status -- read-only, no management buttons
(registering a host is a deliberate, administrative CLI-only action,
`argus agents add`). The Applications list/detail pages show each
application's owning host; no separate remote-only view exists.

## Stack

React 19 + TypeScript + Vite + Tailwind CSS v4 + shadcn/ui-style
components (Radix UI primitives + `class-variance-authority`) +
TanStack Query + React Router + Recharts + React Flow. Vitest + React
Testing Library for tests.

## Local development

Two terminals:

```bash
# Terminal 1 -- the API (from the repo root)
argus-api

# Terminal 2 -- this dashboard
cd web
npm install
npm run dev
```

Then open **http://localhost:5173**. The API is expected at
**http://127.0.0.1:8088** by default (override with
`VITE_ARGUS_API_URL`, e.g. in `web/.env.local`) -- see `src/lib/env.ts`.
Neither server is exposed publicly by default.

## Scripts

```bash
npm run dev      # start the Vite dev server
npm run build    # tsc -b && vite build -- production build to dist/
npm run preview  # preview the production build locally
npm run lint     # eslint .
npm test         # vitest run
```

## Structure

```
src/
├── api/          typed GET-only client (client.ts, types.ts, one module per resource)
├── components/
│   ├── ui/       shadcn/ui-style primitives (button, card, badge, tabs, ...)
│   ├── status/   HealthBadge / DoctorCheckBadge / IncidentBadge / EvidenceSeverityBadge / HostStatusBadge
│   ├── layout/   AppShell, Sidebar, TopBar, ApiOfflineGate
│   ├── charts/   HistoryChart (Recharts)
│   ├── evidence/ Timeline/Evidence lists, citation linking, AI explanation panel
│   └── topology/ ApplicationTopology (React Flow)
├── pages/        one file per route
├── hooks/        TanStack Query hooks (server state) + useTheme
├── lib/          env/format/status/citation/utils -- small, focused helpers
├── realtime/     the one SSE connection (EventSource), event->query invalidation map, connection-state context
└── tests/        shared test utilities, fixtures, and app-wide tests (API client, read-only guard, security)
```
