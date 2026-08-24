"""Milestone 13 -- the FastAPI read layer.

This package is a presentation/read layer over Argus's existing
deterministic read models (``argus.cli.queries``), the evidence bundle
assembler (``argus.evidence.assembler``), and persisted AI explanations
(``argus.store.repository``) -- it defines no new business logic of its
own and decides nothing about health, evidence, or incidents that the
CLI doesn't already decide the same way. See ``argus.api.app`` for the
application factory.

Boundary, mirroring ``argus.ai``'s own docstring:

* Every ``/api/v1`` route is ``GET`` only -- see
  ``tests/unit/test_api_readonly_guard.py`` for the automated proof
  that no mutating HTTP method is ever exposed.
* Normal routes never import ``docker``, ``argus.collectors``, or
  either AI provider SDK (``anthropic``, ``google.genai``) -- see
  ``tests/unit/test_api_architecture_guard.py``. The one deliberate
  exception is ``argus.api.routes.doctor``, which calls Argus's
  existing read-only ``argus.doctor.checks`` subsystem (itself a live,
  but read-only, Docker diagnostic) -- documented there.
* The explanations routes read persisted ``incident_explanations`` rows
  only; they never instantiate an AI provider or make a network call.
"""

from __future__ import annotations
