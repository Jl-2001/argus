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
# argus
