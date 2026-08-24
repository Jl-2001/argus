"""Regression tests for `live_ai_gate.live_ai_test_eligible` -- the
AND-gate a real, billed AI provider smoke test must pass before
`test_ai_smoke.py` lets it run (see that module's own docstring).

Pure function, no fixtures, no Docker, no network, and -- deliberately
-- none of `test_ai_smoke.py`'s own `integration` / `docker` / `ai`
markers, so this file runs as part of the default `-m "not ai"` suite
and is exercised on every offline run, not only when a live AI run is
explicitly requested.
"""

from __future__ import annotations

from tests.integration.live_ai_gate import live_ai_test_eligible


class TestLiveAITestGating:
    def test_key_present_and_live_flag_absent_is_not_eligible(self):
        # A key left over in a developer's shell from unrelated work
        # must never be enough by itself.
        assert live_ai_test_eligible(api_key="sk-ant-real-looking-key", live_flag=None) is False

    def test_key_present_and_live_flag_is_not_exactly_1_is_not_eligible(self):
        for not_one in ("0", "true", "TRUE", "yes", "2", "01", " 1", "1 ", ""):
            assert live_ai_test_eligible(api_key="sk-ant-real-looking-key", live_flag=not_one) is False, (
                f"live_flag={not_one!r} must not be treated as opt-in"
            )

    def test_live_flag_1_and_gemini_key_present_is_eligible(self):
        # ARGUS_RUN_LIVE_AI_TESTS=1 + GEMINI_API_KEY configured -> the
        # Gemini live smoke test becomes eligible to run.
        assert live_ai_test_eligible(api_key="fake-gemini-key", live_flag="1") is True

    def test_live_flag_1_but_anthropic_key_missing_still_skips(self):
        # ARGUS_RUN_LIVE_AI_TESTS=1 alone is not enough either -- the
        # opt-in flag with no Anthropic key still skips the Anthropic
        # test (each provider gates independently on its own key).
        assert live_ai_test_eligible(api_key=None, live_flag="1") is False
        assert live_ai_test_eligible(api_key="", live_flag="1") is False

    def test_key_missing_and_live_flag_absent_is_not_eligible(self):
        assert live_ai_test_eligible(api_key=None, live_flag=None) is False

    def test_key_present_and_live_flag_1_is_eligible(self):
        assert live_ai_test_eligible(api_key="fake-anthropic-key", live_flag="1") is True
