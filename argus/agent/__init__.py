"""``argus-agent`` -- the small, read-only process that runs on a
*remote* monitored machine (Milestone 16).

Deliberately minimal, and deliberately never imported as a whole
package by ``argus.api`` -- only ``argus.agent.protocol`` (the shared
wire contract) is meant to be imported from the control-plane side; see
that module's own docstring. This ``__init__`` re-exports nothing, so
``import argus.agent.protocol`` alone never pulls in this package's
Docker-touching runtime modules (``snapshot``/``client``/``app``).

What this package may import: the read-only Docker collector
(``argus.collectors``), evidence collection/redaction helpers
(``argus.evidence``), plain domain models (``argus.domain``), and its
own protocol/security types. What it must never import: an AI provider,
the dashboard/API, the central incident engine
(``argus.incidents``/``argus.ingestion``), remediation, voice, or any
shell-execution helper -- see ``tests/unit/test_agent_architecture_guard.py``
for the automated guard.
"""
