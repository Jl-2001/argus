"""Architecture guard for Milestone 12/12.1's AI boundary, in both
directions:

1. Nothing below `argus.ai` (domain, evidence, store, collectors,
   collector, incidents) imports `anthropic` OR `google.genai` -- both
   provider SDK dependencies stay strictly above the deterministic
   substrate, regardless of which one the code in question is about.
2. Nothing inside `argus.ai` imports `docker` -- the AI layer has zero
   capability to reach Docker directly; its only input is an already-
   assembled `EvidenceBundle`.
3. Each provider SDK is confined to its own adapter module --
   `anthropic` only in `argus.ai.providers.anthropic`, `google.genai`
   only in `argus.ai.providers.gemini`. `argus.ai.explain`,
   `argus.ai.models`, `argus.ai.prompts`, and `argus.ai.validation`
   never import either SDK directly -- they depend only on the
   provider-agnostic `AIProvider` Protocol.

Also confirms `argus.ai` never imports an agent-framework dependency
(no LangGraph, no LlamaIndex) and never imports a third model SDK
(no OpenAI).
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil

import argus.ai
import argus.collector
import argus.collectors
import argus.domain
import argus.evidence
import argus.incidents
import argus.store


def _module_names(package) -> list[str]:
    """All modules in `package`, recursively -- `pkgutil.iter_modules`
    alone only sees direct children, which would silently skip
    subpackages like `argus.ai.providers` and everything inside it."""

    names = [package.__name__]
    for module_info in pkgutil.walk_packages(package.__path__, prefix=f"{package.__name__}."):
        names.append(module_info.name)
    return names


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


_DETERMINISTIC_PACKAGES = (
    argus.domain, argus.evidence, argus.store, argus.collectors, argus.collector, argus.incidents,
)

_PROVIDER_SDK_ROOTS = {"anthropic", "google"}  # `google` covers `from google import genai`


class TestDeterministicSubstrateNeverImportsAProviderSDK:
    def test_no_deterministic_module_imports_a_provider_sdk(self):
        offenders = []
        for package in _DETERMINISTIC_PACKAGES:
            for module_name in _module_names(package):
                module = importlib.import_module(module_name)
                source = inspect.getsource(module)
                found = _imported_roots(source) & _PROVIDER_SDK_ROOTS
                if found:
                    offenders.append((module_name, found))
        assert not offenders, f"deterministic module(s) import a provider SDK: {offenders}"

    def test_no_deterministic_module_imports_openai_or_agent_frameworks(self):
        forbidden = _PROVIDER_SDK_ROOTS | {"openai", "langgraph", "langchain", "llama_index"}
        offenders = []
        for package in _DETERMINISTIC_PACKAGES:
            for module_name in _module_names(package):
                module = importlib.import_module(module_name)
                source = inspect.getsource(module)
                found = _imported_roots(source) & forbidden
                if found:
                    offenders.append((module_name, found))
        assert not offenders, f"deterministic module(s) import AI dependencies: {offenders}"


class TestAILayerNeverImportsDocker:
    def test_no_ai_module_imports_docker(self):
        offenders = []
        for module_name in _module_names(argus.ai):
            module = importlib.import_module(module_name)
            source = inspect.getsource(module)
            if "docker" in _imported_roots(source):
                offenders.append(module_name)
        assert not offenders, f"argus.ai module(s) import docker: {offenders}"

    def test_no_ai_module_imports_a_third_model_sdk_or_agent_framework(self):
        forbidden = {"openai", "langgraph", "langchain", "llama_index"}
        offenders = []
        for module_name in _module_names(argus.ai):
            module = importlib.import_module(module_name)
            source = inspect.getsource(module)
            found = _imported_roots(source) & forbidden
            if found:
                offenders.append((module_name, found))
        assert not offenders, f"argus.ai module(s) import forbidden dependencies: {offenders}"

    def test_no_ai_module_imports_a_vector_database_or_embeddings_library(self):
        forbidden = {"chromadb", "pinecone", "qdrant_client", "weaviate", "faiss", "sentence_transformers"}
        offenders = []
        for module_name in _module_names(argus.ai):
            module = importlib.import_module(module_name)
            source = inspect.getsource(module)
            found = _imported_roots(source) & forbidden
            if found:
                offenders.append((module_name, found))
        assert not offenders, f"argus.ai module(s) import vector/embedding dependencies: {offenders}"

    def test_anthropic_import_is_confined_to_its_own_provider_adapter(self):
        """`anthropic` itself is of course expected within argus.ai --
        but only in providers/anthropic.py, the one deliberately narrow
        boundary module. Nothing else in argus.ai (not explain.py,
        not models.py/prompts.py/validation.py, and not the gemini
        adapter) should need to import it directly."""

        offenders = []
        for module_name in _module_names(argus.ai):
            if module_name in ("argus.ai", "argus.ai.providers.anthropic"):
                continue
            module = importlib.import_module(module_name)
            source = inspect.getsource(module)
            if "anthropic" in _imported_roots(source):
                offenders.append(module_name)
        assert not offenders, f"anthropic imported outside argus.ai.providers.anthropic: {offenders}"

    def test_google_genai_import_is_confined_to_its_own_provider_adapter(self):
        """Symmetric guard for the second provider: `google` (i.e.
        `from google import genai`) is expected only in
        providers/gemini.py -- never in explain.py, models.py,
        prompts.py, validation.py, or the anthropic adapter."""

        offenders = []
        for module_name in _module_names(argus.ai):
            if module_name in ("argus.ai", "argus.ai.providers.gemini"):
                continue
            module = importlib.import_module(module_name)
            source = inspect.getsource(module)
            if "google" in _imported_roots(source):
                offenders.append(module_name)
        assert not offenders, f"google.genai imported outside argus.ai.providers.gemini: {offenders}"


class TestPackageDiscovery:
    def test_every_deterministic_package_is_actually_discovered(self):
        for package in _DETERMINISTIC_PACKAGES:
            assert len(_module_names(package)) >= 1

    def test_ai_package_has_the_expected_modules(self):
        names = {name.rsplit(".", 1)[-1] for name in _module_names(argus.ai)}
        assert {"models", "prompts", "validation", "explain", "providers", "anthropic", "gemini", "base"} <= names

    def test_module_name_recursion_actually_reaches_the_providers_subpackage(self):
        # guards against silently reverting `_module_names` to the
        # non-recursive `pkgutil.iter_modules` and having every check
        # above pass vacuously because providers/*.py was never scanned.
        names = set(_module_names(argus.ai))
        assert "argus.ai.providers.anthropic" in names
        assert "argus.ai.providers.gemini" in names
