"""Milestone 15 -- the realtime "something changed" layer.

This package never computes health, never decides an incident's
lifecycle, and never talks to Docker or an AI provider -- it only turns
already-committed writes other layers made (``argus.collector.loop``,
``argus.incidents.engine``, ``argus.ai.explain``) into small, sanitized
rows in the ``realtime_events`` table (see ``argus.store.repository``),
which ``argus.api.routes.events`` then streams to the browser over SSE.

The one rule that matters everywhere in this package: an event is
*never* authoritative state, and writing one is *never* allowed to fail
the write it describes -- see ``emitter.py``'s own docstring for both
halves of that in detail.
"""

from __future__ import annotations
