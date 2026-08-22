"""Long-running orchestration: schedule discover() -> evaluate -> persist.

This package (singular ``collector``) is distinct from
``argus.collectors`` (plural -- Docker discovery, Milestone 3). It may
import ``argus.collectors``, ``argus.domain``, and ``argus.store``. It
must never import Docker's SDK directly (all Docker access flows
through ``argus.collectors.docker_client.DockerClient``), and never an
AI client, web framework, or metrics/tracing library -- see
``tests/unit/test_collector_loop.py``'s architecture guard.

Nothing here computes health, detects an incident, or decides
anything about what a status *means* -- it only schedules and wires
together work the domain/collectors/store layers already know how to
do.
"""
