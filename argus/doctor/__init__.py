"""Live prerequisite diagnostics: "can Argus currently monitor correctly?"

Unlike every other CLI surface (``argus status``/``apps``/``inspect``/
``incidents``/``history``), which read *persisted* state only, this
package performs live checks: Docker reachability, database openability,
collector freshness, clock sanity. It is deliberately independent of
``argus.cli`` -- ``run_checks`` returns a plain, serializable
``DoctorResult`` so any future surface (a dashboard's own "Argus self
health" panel, for instance) can reuse the same diagnostics without
depending on CLI text formatting.

May import ``argus.collectors.docker_client`` (the existing read-only
adapter -- never a second Docker connection implementation) and
``argus.store``/``argus.domain``. Must never import ``anthropic``,
``openai``, ``langgraph``, or ``fastapi``, and must never call a
mutating Docker method. See ``tests/unit/test_doctor.py``'s
architecture/read-only guards.

Doctor diagnoses. It never repairs: it does not create a missing
database, run a migration, start the collector, or touch Docker beyond
a read.
"""
