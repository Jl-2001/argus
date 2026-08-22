"""Argus's command-line interface -- a read-only presentation layer.

May import ``argus.store`` and ``argus.domain`` (including
``argus.domain.health`` for the `HealthRules` staleness threshold --
never an ``evaluate_*`` function). Must never import ``docker``,
``argus.collectors``, or ``argus.incidents.engine``: the CLI queries
and formats already-persisted state, it never discovers, evaluates, or
mutates anything. See ``tests/unit/test_cli_commands.py``'s
architecture guard.

The one exception, reserved for Milestone 8, is ``argus doctor`` --
live checks are not implemented yet.
"""
