"""The single gate a real, billed AI provider smoke test must pass
before it is allowed to run -- imported by both `test_ai_smoke.py`
(which gates `TestRealClaudeSmokeTest`/`TestRealGeminiSmokeTest` on it)
and its own regression tests in `test_live_ai_gating.py`, so there is
exactly one definition of "eligible" rather than two that could drift
apart.

A provider's own API key being present in the environment is
deliberately NOT sufficient on its own: a developer's shell can have
`ANTHROPIC_API_KEY` or `GEMINI_API_KEY` left over from unrelated work,
and a plain `python -m pytest` (no `-m ai`, no opt-in flag) must never
spend real API credits just because that happened to be true. Running
a live smoke test requires an explicit, second, purpose-built opt-in:
`ARGUS_RUN_LIVE_AI_TESTS=1`.

Not a test module itself (no `test_` prefix) -- pytest never collects
it directly.
"""

from __future__ import annotations

from typing import Optional

__all__ = ["live_ai_test_eligible"]


def live_ai_test_eligible(*, api_key: Optional[str], live_flag: Optional[str]) -> bool:
    """True only when both a real API key is configured AND the live
    opt-in flag is exactly the string ``"1"`` -- not ``"true"``, not
    ``"yes"``, not any other truthy-looking value, so a malformed or
    accidental value fails closed (skips) rather than open (spends
    money)."""

    return bool(api_key) and live_flag == "1"
