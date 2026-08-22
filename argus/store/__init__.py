"""Durable SQLite persistence for Argus's identities and observation history.

This package may import ``sqlite3`` and ``argus.domain``. It must never
import Docker, an AI client, or a web framework -- see the architecture
guard in tests/unit/test_repository.py. It also never imports
``argus.domain.health`` or calls any ``evaluate_*`` function: the
repository persists health values the domain layer already computed,
it never computes them itself.
"""
