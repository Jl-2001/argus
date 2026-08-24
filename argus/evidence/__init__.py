"""Milestone 10 -- deterministic log/evidence collection.

Turns raw container log lines and Docker facts into structured,
bounded, redacted ``argus.domain.models.EvidenceRecord`` rows, and
associates them with incidents by time proximity only.

Nothing in this package is AI. It never imports ``anthropic``,
``openai``, or ``langgraph``; it never calls a semantic embedding model
or a local LLM; it never makes a network call to a model API. See
``tests/unit/test_evidence_architecture_guard.py`` for the automated
guard. The evidence this package produces is *input* a future milestone
may hand to a model -- this package itself only ever does regex
matching, deterministic aggregation, and timestamp-window comparisons.
"""

from __future__ import annotations
