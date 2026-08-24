"""Milestone 12 -- Claude evidence-grounded incident explanation.

This is the only package in Argus allowed to import the Anthropic SDK
(and the only place the ``ANTHROPIC_API_KEY`` environment variable is
ever read). Everything below this package -- ``argus.domain``,
``argus.evidence``, ``argus.store``, ``argus.collectors``,
``argus.collector``, ``argus.incidents`` -- remains 100% deterministic
and has no idea a model exists. See
``tests/unit/test_ai_architecture_guard.py`` for the automated guard
enforcing both directions of that boundary: nothing below this package
imports ``anthropic``, and nothing in this package imports ``docker``.

Claude is a narrator, analyst, and advisor over an already-assembled,
already-bounded ``argus.evidence.bundle.EvidenceBundle`` (Milestone 11).
It is never a sensor (it cannot query Docker or SQLite), never an
actuator (it cannot mutate anything), and never a monitor (core health
monitoring runs whether or not this package is even configured). A
missing or invalid ``ANTHROPIC_API_KEY``, a network failure, or an
invalid model response can only ever fail *this* package's own
operations -- never core Argus monitoring.
"""

from __future__ import annotations
