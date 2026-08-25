"""The one shared "persist a snapshot of domain facts, then run the
central incident engine over it" pipeline -- Milestone 16.

Deliberately its own small package, sitting between
``argus.store``/``argus.incidents`` and its two callers
(``argus.collector.loop`` for the local machine,
``argus.api.routes.agents`` for a remote agent's ingested snapshot) --
see ``argus.ingestion.pipeline`` for why this is the one place that
logic lives, not duplicated between them.
"""
