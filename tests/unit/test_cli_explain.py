"""End-to-end tests for `argus explain`: invoke argus.cli.main.main()
with argv, assert on captured stdout/stderr and exit codes. The AI
provider is monkeypatched at the CLI's own resolution point
(`argus.cli.commands.explain.resolve_provider`) -- no real network call
anywhere in this file.
"""

from __future__ import annotations

import ast
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from argus.ai.providers.anthropic import AnthropicProvider
from argus.ai.providers.base import AIConfigurationError
from argus.ai.providers.gemini import GeminiProvider
from argus.cli import main as main_module
from argus.cli.commands import explain as explain_module
from argus.domain.models import HealthStatus
from argus.store.database import open_database
from argus.store.repository import Repository

UTC = timezone.utc
NOW = datetime.now(UTC)


def run_cli(db_path: Path, *args: str) -> int:
    return main_module.main(["--database", str(db_path), *args])


def seed_incident(tmp_path):
    db_path = tmp_path / "a.db"
    conn = open_database(db_path)
    repo = Repository(conn)
    app_id = repo.upsert_application(key="cnstrct", name="CNSTRCT", is_standalone=False, observed_at=NOW - timedelta(minutes=5))
    t = repo.insert_transition(scope="application", scope_id=app_id, from_status=None, to_status=HealthStatus.UNHEALTHY, occurred_at=NOW - timedelta(minutes=1))
    incident_id = repo.open_incident(
        scope_id=app_id, failure_signature="application:cnstrct", opened_at=NOW - timedelta(minutes=1),
        opening_status=HealthStatus.UNHEALTHY, opening_transition_id=t,
    )
    conn.close()
    return db_path, incident_id


# --------------------------------------------------------------------------
# Fake Anthropic SDK plumbing (reused from test_ai_explain.py's shape)
# --------------------------------------------------------------------------


class _FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, input_dict):
        self.input = input_dict


class _FakeUsage:
    input_tokens = 111
    output_tokens = 22


class _FakeMessage:
    def __init__(self, content):
        self.content = content
        self.usage = _FakeUsage()
        self.model = "claude-sonnet-5"


class _ScriptedMessagesAPI:
    def __init__(self, response_dict):
        self._response = response_dict
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeMessage([_FakeToolUseBlock(self._response)])


class _ScriptedSDKClient:
    def __init__(self, response_dict):
        self.messages = _ScriptedMessagesAPI(response_dict)


def _explanation_response(incident_id: int, **overrides) -> dict:
    response = {
        "incident_id": incident_id,
        "summary": "The API became unhealthy following repeated database connection failures.",
        "root_cause_claim": {"text": "PostgreSQL instability is the most likely cause.", "evidence_references": [f"incident:{incident_id}"]},
        "supporting_claims": [],
        "confidence": "medium",
        "recommendation": {"category": "check_database", "explanation": "Inspect PostgreSQL restart behavior."},
        "caveats": ["Temporal correlation alone does not establish causation."],
    }
    response.update(overrides)
    return response


def patch_working_client(monkeypatch, incident_id: int, **overrides):
    response = _explanation_response(incident_id, **overrides)
    sdk = _ScriptedSDKClient(response)
    provider = AnthropicProvider(sdk_client=sdk)
    monkeypatch.setattr(explain_module, "resolve_provider", lambda config: provider)
    return sdk


# --------------------------------------------------------------------------
# Fake Gemini SDK plumbing
# --------------------------------------------------------------------------


class _FakeGeminiUsageMetadata:
    prompt_token_count = 333
    candidates_token_count = 44


class _FakeGeminiResponse:
    def __init__(self, text):
        self.text = text
        self.usage_metadata = _FakeGeminiUsageMetadata()


class _ScriptedGeminiModelsAPI:
    def __init__(self, response_dict):
        import json as _json

        self._text = _json.dumps(response_dict)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeGeminiResponse(self._text)


class _ScriptedGeminiSDKClient:
    def __init__(self, response_dict):
        self.models = _ScriptedGeminiModelsAPI(response_dict)


def patch_working_gemini_client(monkeypatch, incident_id: int, **overrides):
    response = _explanation_response(incident_id, **overrides)
    sdk = _ScriptedGeminiSDKClient(response)
    provider = GeminiProvider(sdk_client=sdk)
    monkeypatch.setattr(explain_module, "resolve_provider", lambda config: provider)
    return sdk


def patch_missing_key(monkeypatch):
    def boom(config):
        raise AIConfigurationError("Argus AI unavailable: ANTHROPIC_API_KEY is not configured.")

    monkeypatch.setattr(explain_module, "resolve_provider", boom)


class TestMissingApiKey:
    def test_explain_fails_cleanly_without_a_key(self, tmp_path, capsys):
        db_path, incident_id = seed_incident(tmp_path)

        code = run_cli(db_path, "explain", str(incident_id))
        captured = capsys.readouterr()
        assert code == 1
        assert "ANTHROPIC_API_KEY" in captured.err
        assert "Traceback" not in captured.err

    def test_other_commands_unaffected_by_missing_key(self, tmp_path, capsys):
        db_path, incident_id = seed_incident(tmp_path)
        for args in (["status"], ["apps"], ["incidents"]):
            code = run_cli(db_path, *args)
            assert code == 0


class TestHumanOutput:
    def test_summary_and_required_sections_present(self, tmp_path, capsys, monkeypatch):
        db_path, incident_id = seed_incident(tmp_path)
        patch_working_client(monkeypatch, incident_id)

        code = run_cli(db_path, "explain", str(incident_id))
        out = capsys.readouterr().out
        assert code == 0
        assert f"INCIDENT #{incident_id}" in out
        assert "CNSTRCT" in out
        assert "Summary" in out
        assert "Probable root cause" in out
        assert "Confidence" in out
        assert "MEDIUM" in out
        assert "Recommended next step" in out
        assert "Caveats" in out

    def test_second_invocation_is_cached(self, tmp_path, capsys, monkeypatch):
        db_path, incident_id = seed_incident(tmp_path)
        sdk = patch_working_client(monkeypatch, incident_id)

        run_cli(db_path, "explain", str(incident_id))
        capsys.readouterr()
        run_cli(db_path, "explain", str(incident_id))
        out = capsys.readouterr().out
        assert "cached" in out.lower()
        assert len(sdk.messages.calls) == 1


class TestMinimalExplanation:
    def test_no_root_cause_no_recommendation_no_caveats_still_renders(self, tmp_path, capsys, monkeypatch):
        db_path, incident_id = seed_incident(tmp_path)
        patch_working_client(
            monkeypatch, incident_id,
            root_cause_claim=None, recommendation=None, caveats=[], confidence="low",
        )

        code = run_cli(db_path, "explain", str(incident_id))
        out = capsys.readouterr().out
        assert code == 0
        assert f"INCIDENT #{incident_id}" in out
        assert "Summary" in out
        assert "Confidence" in out
        assert "LOW" in out
        assert "Probable root cause" not in out
        assert "Recommended next step" not in out
        assert "Caveats" not in out


class TestJsonOutput:
    def test_json_is_parseable_and_shaped_correctly(self, tmp_path, capsys, monkeypatch):
        db_path, incident_id = seed_incident(tmp_path)
        patch_working_client(monkeypatch, incident_id)

        run_cli(db_path, "explain", str(incident_id), "--json")
        payload = json.loads(capsys.readouterr().out)

        assert payload["incident_id"] == incident_id
        assert payload["cached"] is False
        assert "bundle_fingerprint" in payload
        assert payload["usage"] == {"input_tokens": 111, "output_tokens": 22}
        assert payload["explanation"]["confidence"] == "medium"

    def test_json_never_includes_api_key_or_system_prompt(self, tmp_path, capsys, monkeypatch):
        db_path, incident_id = seed_incident(tmp_path)
        patch_working_client(monkeypatch, incident_id)

        run_cli(db_path, "explain", str(incident_id), "--json")
        raw = capsys.readouterr().out
        assert "ANTHROPIC_API_KEY" not in raw
        assert "sk-ant" not in raw
        assert "incident-analysis layer for Argus" not in raw  # a phrase from SYSTEM_PROMPT

    def test_cached_json_call_makes_no_additional_api_call(self, tmp_path, capsys, monkeypatch):
        db_path, incident_id = seed_incident(tmp_path)
        sdk = patch_working_client(monkeypatch, incident_id)

        run_cli(db_path, "explain", str(incident_id), "--json")
        capsys.readouterr()
        run_cli(db_path, "explain", str(incident_id), "--json")
        payload = json.loads(capsys.readouterr().out)
        assert payload["cached"] is True
        assert len(sdk.messages.calls) == 1


class TestForceRefreshFlag:
    def test_force_refresh_makes_a_second_call(self, tmp_path, capsys, monkeypatch):
        db_path, incident_id = seed_incident(tmp_path)
        sdk = patch_working_client(monkeypatch, incident_id)

        run_cli(db_path, "explain", str(incident_id))
        capsys.readouterr()
        run_cli(db_path, "explain", str(incident_id), "--force-refresh")
        capsys.readouterr()
        assert len(sdk.messages.calls) == 2


class TestProviderFlag:
    def test_provider_anthropic_explicit_flag(self, tmp_path, capsys, monkeypatch):
        db_path, incident_id = seed_incident(tmp_path)
        patch_working_client(monkeypatch, incident_id)

        code = run_cli(db_path, "explain", str(incident_id), "--provider", "anthropic")
        out = capsys.readouterr().out
        assert code == 0
        assert "Provider   anthropic" in out

    def test_provider_gemini_explicit_flag(self, tmp_path, capsys, monkeypatch):
        db_path, incident_id = seed_incident(tmp_path)
        patch_working_gemini_client(monkeypatch, incident_id)

        code = run_cli(db_path, "explain", str(incident_id), "--provider", "gemini")
        out = capsys.readouterr().out
        assert code == 0
        assert "Provider   gemini" in out
        assert "Model      gemini" in out

    def test_gemini_and_anthropic_are_independent_cache_entries_via_cli(self, tmp_path, capsys, monkeypatch):
        db_path, incident_id = seed_incident(tmp_path)

        anthropic_sdk = None

        def resolve(config):
            from argus.ai.providers.base import AIProviderName

            if config.provider is AIProviderName.GEMINI:
                sdk = _ScriptedGeminiSDKClient(_explanation_response(incident_id, summary="gemini summary"))
                return GeminiProvider(sdk_client=sdk)
            sdk = _ScriptedSDKClient(_explanation_response(incident_id, summary="claude summary"))
            return AnthropicProvider(sdk_client=sdk)

        monkeypatch.setattr(explain_module, "resolve_provider", resolve)

        code_a = run_cli(db_path, "explain", str(incident_id), "--provider", "anthropic", "--json")
        payload_a = json.loads(capsys.readouterr().out)
        code_g = run_cli(db_path, "explain", str(incident_id), "--provider", "gemini", "--json")
        payload_g = json.loads(capsys.readouterr().out)

        assert code_a == 0 and code_g == 0
        assert payload_a["cached"] is False
        assert payload_g["cached"] is False  # Gemini's first call is not a hit against Claude's entry
        assert payload_a["explanation"]["summary"] == "claude summary"
        assert payload_g["explanation"]["summary"] == "gemini summary"

    def test_env_var_selects_provider_when_no_flag_given(self, tmp_path, capsys, monkeypatch):
        db_path, incident_id = seed_incident(tmp_path)
        patch_working_gemini_client(monkeypatch, incident_id)
        monkeypatch.setenv("ARGUS_AI_PROVIDER", "gemini")

        code = run_cli(db_path, "explain", str(incident_id))
        out = capsys.readouterr().out
        assert code == 0
        assert "Provider   gemini" in out

    def test_explicit_flag_overrides_env_var(self, tmp_path, capsys, monkeypatch):
        db_path, incident_id = seed_incident(tmp_path)
        patch_working_client(monkeypatch, incident_id)
        monkeypatch.setenv("ARGUS_AI_PROVIDER", "gemini")  # flag below must win

        code = run_cli(db_path, "explain", str(incident_id), "--provider", "anthropic")
        out = capsys.readouterr().out
        assert code == 0
        assert "Provider   anthropic" in out

    def test_default_provider_without_flag_or_env_is_anthropic(self, tmp_path, capsys, monkeypatch):
        db_path, incident_id = seed_incident(tmp_path)
        monkeypatch.delenv("ARGUS_AI_PROVIDER", raising=False)
        patch_working_client(monkeypatch, incident_id)

        code = run_cli(db_path, "explain", str(incident_id))
        out = capsys.readouterr().out
        assert code == 0
        assert "Provider   anthropic" in out


class TestNonexistentIncident:
    def test_exits_1_with_clear_message_no_traceback(self, tmp_path, capsys, monkeypatch):
        db_path, incident_id = seed_incident(tmp_path)
        patch_working_client(monkeypatch, incident_id)

        code = run_cli(db_path, "explain", "999999")
        captured = capsys.readouterr()
        assert code == 1
        assert "999999" in captured.err
        assert "Traceback" not in captured.err


class TestInvalidResponseFromModel:
    def test_fabricated_reference_twice_fails_cleanly(self, tmp_path, capsys, monkeypatch):
        db_path, incident_id = seed_incident(tmp_path)
        patch_working_client(
            monkeypatch, incident_id,
            root_cause_claim={"text": "fake", "evidence_references": ["log_signal:9999"]},
        )

        code = run_cli(db_path, "explain", str(incident_id))
        captured = capsys.readouterr()
        assert code == 1
        assert "invalid" in captured.err.lower() or "rejected" in captured.err.lower()
        assert "Traceback" not in captured.err


class TestHelp:
    def test_explain_listed_in_top_level_help(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main_module.main(["--help"])
        assert exc_info.value.code == 0
        assert "explain" in capsys.readouterr().out

    def test_explain_help_lists_flags(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main_module.main(["explain", "--help"])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "--json" in out
        assert "--force-refresh" in out


# --------------------------------------------------------------------------
# Architecture guard -- explain.py's own direct imports
# --------------------------------------------------------------------------

FORBIDDEN_DIRECT_IMPORT_ROOTS = {"docker", "openai", "langgraph", "fastapi", "requests", "httpx"}


def _imported_roots(source: str) -> set[str]:
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


class TestArchitectureGuard:
    def test_explain_command_module_has_no_forbidden_direct_imports(self):
        source = inspect.getsource(explain_module)
        found = _imported_roots(source) & FORBIDDEN_DIRECT_IMPORT_ROOTS
        assert not found, f"explain.py imports forbidden module(s): {found}"

    def test_explain_command_never_calls_docker_or_health_evaluators(self):
        source = inspect.getsource(explain_module)
        tree = ast.parse(source)
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    called.add(func.attr)
        forbidden = {"evaluate_container_health", "evaluate_service_health", "evaluate_application_health", "discover"}
        assert not (called & forbidden)
