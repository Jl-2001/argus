"""Deterministic transition detection and application-level incidents.

This package may import ``argus.domain`` and ``argus.store``. It must
never import Docker, an AI client, a web framework, or
``argus.collectors`` directly -- incident processing operates on
already-persisted/domain state (the same ``Application`` objects and
per-container statuses the collector loop already has on hand), never
on Docker itself. See ``tests/unit/test_incident_engine.py``'s
architecture guard.

It never sends a notification, calls an AI model, or performs
remediation -- it only observes state that already exists and records
what changed.
"""
