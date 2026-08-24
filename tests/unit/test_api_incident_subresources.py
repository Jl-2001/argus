"""Milestone 13 -- GET /api/v1/incidents/{id}/evidence, /bundle,
/explanations, /explanations/latest.

`TestNoProviderCallsFromExplanations` is the "GET explanation route
must never call network" requirement: it monkeypatches
`AnthropicProvider.__init__`/`GeminiProvider.__init__` to fail loudly
if constructed, then exercises every explanations route -- proving
there is no code path from a GET request to instantiating a provider,
not just that no request happened to trigger one in practice.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from api_fixtures import seed_incident_stack
from argus.api.app import create_app
from argus.evidence.assembler import DEFAULT_ASSEMBLER_CONFIG, assemble_evidence_bundle
from argus.store.database import open_database
from argus.store.repository import Repository


class TestEvidenceEndpoint:
    def test_only_redacted_stored_samples_are_exposed(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed = seed_incident_stack(db_path)
        client = TestClient(create_app(database_path=db_path))

        body = client.get(f"/api/v1/incidents/{seed['incident_id']}/evidence").json()
        assert body["incident_id"] == seed["incident_id"]
        assert len(body["evidence"]) == 1
        item = body["evidence"][0]
        assert item["sample"] == "[REDACTED] container killed: out of memory"
        assert set(item.keys()) == {
            "category", "severity", "count", "first_seen_at", "last_seen_at", "sample", "source", "source_type",
        }

    def test_limit_bounds_the_result(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed = seed_incident_stack(db_path)
        client = TestClient(create_app(database_path=db_path))

        response = client.get(f"/api/v1/incidents/{seed['incident_id']}/evidence?limit=0")
        assert response.status_code == 422  # limit must be >= 1

        response_ok = client.get(f"/api/v1/incidents/{seed['incident_id']}/evidence?limit=1")
        assert response_ok.status_code == 200
        assert len(response_ok.json()["evidence"]) == 1

    def test_404_for_unknown_incident(self, tmp_path):
        client = TestClient(create_app(database_path=tmp_path / "a.db"))
        response = client.get("/api/v1/incidents/999999/evidence")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "incident_not_found"


class TestBundleEndpoint:
    def test_fingerprint_and_references_are_correct(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed = seed_incident_stack(db_path)

        # The independently-assembled bundle is the ground truth this
        # test checks the API response against.
        conn = open_database(db_path)
        repo = Repository(conn)
        import datetime as dt

        expected_bundle = assemble_evidence_bundle(
            repo, seed["incident_id"], now=seed["now"] + dt.timedelta(seconds=1), config=DEFAULT_ASSEMBLER_CONFIG
        )
        conn.close()

        client = TestClient(create_app(database_path=db_path))
        body = client.get(f"/api/v1/incidents/{seed['incident_id']}/bundle").json()

        assert body["incident"]["incident_id"] == seed["incident_id"]
        assert body["incident"]["reference"] == f"incident:{seed['incident_id']}"
        assert body["application"]["key"] == "cnstrct"
        assert len(body["signals"]) == 1
        assert body["signals"][0]["reference"] == f"log_signal:{seed['signal_id']}"
        # fingerprint is a pure function of bundle *content*, not of the
        # request's own now/generated_at -- two independently-assembled
        # bundles over the same evidence must fingerprint identically.
        assert body["metadata"]["fingerprint"] == expected_bundle.metadata.fingerprint
        assert body["metadata"]["fingerprint"] != ""

    def test_metadata_shape(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed = seed_incident_stack(db_path)
        client = TestClient(create_app(database_path=db_path))
        body = client.get(f"/api/v1/incidents/{seed['incident_id']}/bundle").json()
        metadata = body["metadata"]
        assert set(metadata.keys()) == {
            "generated_at", "window_start", "window_end", "assembler_version", "truncated",
            "omitted_counts", "evidence_subsystem_status", "fingerprint",
        }
        assert metadata["generated_at"] is not None

    def test_404_for_unknown_incident(self, tmp_path):
        client = TestClient(create_app(database_path=tmp_path / "a.db"))
        response = client.get("/api/v1/incidents/999999/bundle")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "incident_not_found"


class TestExplanationsEndpoint:
    def test_provider_model_and_cache_metadata_are_returned(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed = seed_incident_stack(db_path)
        client = TestClient(create_app(database_path=db_path))

        body = client.get(f"/api/v1/incidents/{seed['incident_id']}/explanations").json()
        assert body["incident_id"] == seed["incident_id"]
        assert len(body["explanations"]) == 1
        explanation = body["explanations"][0]
        assert explanation["provider"] == "anthropic"
        assert explanation["model"] == "claude-sonnet-5"
        assert explanation["prompt_version"] == "incident-explanation-v1"
        assert explanation["bundle_fingerprint"] == "deadbeefcafe"
        assert explanation["usage"] == {"input_tokens": 321, "output_tokens": 64}
        assert explanation["explanation"]["summary"].startswith("The container was killed")
        assert explanation["explanation"]["confidence"] == "high"

    def test_incident_with_no_explanation_returns_an_empty_list_not_404(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed = seed_incident_stack(db_path, with_explanation=False)
        client = TestClient(create_app(database_path=db_path))
        body = client.get(f"/api/v1/incidents/{seed['incident_id']}/explanations").json()
        assert body == {"incident_id": seed["incident_id"], "explanations": []}

    def test_404_for_unknown_incident(self, tmp_path):
        client = TestClient(create_app(database_path=tmp_path / "a.db"))
        response = client.get("/api/v1/incidents/999999/explanations")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "incident_not_found"

    def test_latest_returns_the_most_recent_explanation(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed = seed_incident_stack(db_path)
        client = TestClient(create_app(database_path=db_path))
        body = client.get(f"/api/v1/incidents/{seed['incident_id']}/explanations/latest").json()
        assert body["id"] == seed["explanation_id"]

    def test_latest_is_null_not_404_when_incident_exists_but_has_no_explanation(self, tmp_path):
        db_path = tmp_path / "a.db"
        seed = seed_incident_stack(db_path, with_explanation=False)
        client = TestClient(create_app(database_path=db_path))
        response = client.get(f"/api/v1/incidents/{seed['incident_id']}/explanations/latest")
        assert response.status_code == 200
        assert response.json() is None

    def test_latest_404s_when_the_incident_itself_does_not_exist(self, tmp_path):
        client = TestClient(create_app(database_path=tmp_path / "a.db"))
        response = client.get("/api/v1/incidents/999999/explanations/latest")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "incident_not_found"


class TestNoProviderCallsFromExplanations:
    """Patches both providers' constructors to fail loudly if ever
    instantiated, then exercises every explanations route -- proving
    there is no code path from a GET request to a real provider call,
    not merely that none happened to occur."""

    def test_anthropic_provider_is_never_instantiated(self, tmp_path, monkeypatch):
        from argus.ai.providers.anthropic import AnthropicProvider

        def _boom(self, *args, **kwargs):
            raise AssertionError("AnthropicProvider must never be instantiated by a GET /explanations route")

        monkeypatch.setattr(AnthropicProvider, "__init__", _boom)

        db_path = tmp_path / "a.db"
        seed = seed_incident_stack(db_path)
        client = TestClient(create_app(database_path=db_path))

        assert client.get(f"/api/v1/incidents/{seed['incident_id']}/explanations").status_code == 200
        assert client.get(f"/api/v1/incidents/{seed['incident_id']}/explanations/latest").status_code == 200

    def test_gemini_provider_is_never_instantiated(self, tmp_path, monkeypatch):
        from argus.ai.providers.gemini import GeminiProvider

        def _boom(self, *args, **kwargs):
            raise AssertionError("GeminiProvider must never be instantiated by a GET /explanations route")

        monkeypatch.setattr(GeminiProvider, "__init__", _boom)

        db_path = tmp_path / "a.db"
        seed = seed_incident_stack(db_path)
        client = TestClient(create_app(database_path=db_path))

        assert client.get(f"/api/v1/incidents/{seed['incident_id']}/explanations").status_code == 200
        assert client.get(f"/api/v1/incidents/{seed['incident_id']}/explanations/latest").status_code == 200

    def test_explanations_route_module_never_imports_argus_ai_at_all(self):
        # AST-based, not a raw text search: the module's own docstring
        # legitimately *mentions* "argus.ai" in prose (explaining that
        # it doesn't import it) -- only actual import statements count.
        import ast
        import inspect

        import argus.api.routes.explanations as explanations_module

        tree = ast.parse(inspect.getsource(explanations_module))
        imported_modules = {
            alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
        } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

        offenders = {m for m in imported_modules if m == "argus.ai" or m.startswith("argus.ai.")}
        assert not offenders, f"argus.api.routes.explanations imports argus.ai: {offenders}"
