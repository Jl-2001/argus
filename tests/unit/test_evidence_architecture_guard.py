"""Architecture guard for the whole argus.evidence package: no AI
imports anywhere, no semantic/embedding model, no network calls to a
model API. This is Milestone 10's own version of the guard pattern
every prior milestone already applies to its own package."""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil

import argus.evidence

FORBIDDEN_IMPORT_ROOTS = {
    "anthropic",
    "openai",
    "langgraph",
    "langchain",
    "transformers",
    "sentence_transformers",
    "requests",
    "httpx",
    "urllib3",
}

_FORBIDDEN_CALL_NAMES = {
    "embed",
    "embeddings",
    "chat",
    "complete",
    "completion",
    "generate",
}


def _evidence_module_names() -> list[str]:
    names = ["argus.evidence"]
    for module_info in pkgutil.iter_modules(argus.evidence.__path__, prefix="argus.evidence."):
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


def _called_function_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


class TestNoAIImportsAnywhereInEvidencePackage:
    def test_no_forbidden_imports(self):
        for module_name in _evidence_module_names():
            module = importlib.import_module(module_name)
            source = inspect.getsource(module)
            found = _imported_roots(source) & FORBIDDEN_IMPORT_ROOTS
            assert not found, f"{module_name} imports forbidden module(s): {found}"

    def test_no_semantic_or_llm_call_names(self):
        for module_name in _evidence_module_names():
            module = importlib.import_module(module_name)
            source = inspect.getsource(module)
            found = _called_function_names(source) & _FORBIDDEN_CALL_NAMES
            assert not found, f"{module_name} calls suspicious function(s): {found}"

    def test_every_evidence_module_is_actually_discovered(self):
        # Sanity check on the test itself -- if this ever drops to 0 or 1,
        # the package layout changed and this guard silently stopped
        # checking most of it.
        assert len(_evidence_module_names()) >= 6
