"""Milestone 13 -- import-boundary architecture guard for `argus.api`,
mirroring `tests/unit/test_ai_architecture_guard.py`'s own AST-based
approach:

1. No normal `argus.api` module (the package itself, or any
   `argus.api.routes.*` module other than `doctor`) imports `docker`,
   `argus.collectors`, `argus.doctor`, `anthropic`, or `google`
   (`google.genai`).
2. `argus.api.routes.doctor` is the *one* named, deliberate exception --
   it is allowed to import `argus.doctor`/`argus.collectors`/`docker`
   (transitively, via `argus.doctor.checks`), and this test asserts
   that it actually does, so the exception can never silently become
   unnecessary/stale.
3. `argus.api.routes.explanations` (and every other route module) never
   imports `argus.ai` at all -- the explanations routes read persisted
   `ExplanationRecord`s directly; nothing in this API instantiates an
   AI provider.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil

import argus.api

_FORBIDDEN_FOR_NORMAL_ROUTES = {"docker", "anthropic", "google"}
_FORBIDDEN_ROOTS = {"docker", "anthropic", "google"}


def _module_names(package) -> list[str]:
    names = [package.__name__]
    for module_info in pkgutil.walk_packages(package.__path__, prefix=f"{package.__name__}."):
        names.append(module_info.name)
    return names


def _strip_type_checking_blocks(tree: ast.Module) -> ast.Module:
    """Removes the body of every `if TYPE_CHECKING:` (or
    `typing.TYPE_CHECKING`) block before the caller walks the tree for
    imports -- an import guarded that way is never executed at runtime
    (see `argus.api.models`'s own `DoctorResult` import), so it must not
    count as a real runtime import boundary violation, the same way
    Python itself never executes it."""

    class _Stripper(ast.NodeTransformer):
        def visit_If(self, node: ast.If) -> ast.If:
            test = node.test
            is_type_checking = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
                isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
            )
            if is_type_checking:
                node.body = []
            self.generic_visit(node)
            return node

    return _Stripper().visit(tree)


def _imported_roots(source: str) -> set[str]:
    tree = _strip_type_checking_blocks(ast.parse(source))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _imported_dotted_prefixes(source: str) -> set[str]:
    """Every `import a.b.c` / `from a.b.c import ...` as its full dotted
    module path -- used to detect `argus.collectors`/`argus.doctor`
    specifically, not just the `argus` root (which every module here
    imports constantly and harmlessly, e.g. `argus.store.repository`)."""

    tree = _strip_type_checking_blocks(ast.parse(source))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


class TestNormalAPIModulesNeverImportDockerOrProviderSDKs:
    def test_no_normal_api_module_imports_docker_or_a_provider_sdk(self):
        offenders = []
        for module_name in _module_names(argus.api):
            if module_name in ("argus.api.routes.doctor",):
                continue  # the one documented exception
            module = importlib.import_module(module_name)
            source = inspect.getsource(module)
            found = _imported_roots(source) & _FORBIDDEN_FOR_NORMAL_ROUTES
            if found:
                offenders.append((module_name, found))
        assert not offenders, f"normal argus.api module(s) import a forbidden dependency: {offenders}"

    def test_no_normal_api_module_imports_argus_collectors_or_argus_doctor(self):
        offenders = []
        for module_name in _module_names(argus.api):
            if module_name in ("argus.api.routes.doctor",):
                continue
            module = importlib.import_module(module_name)
            source = inspect.getsource(module)
            dotted = _imported_dotted_prefixes(source)
            found = {m for m in dotted if m == "argus.collectors" or m.startswith("argus.collectors.")}
            found |= {m for m in dotted if m == "argus.doctor" or m.startswith("argus.doctor.")}
            if found:
                offenders.append((module_name, found))
        assert not offenders, f"normal argus.api module(s) import argus.collectors/argus.doctor: {offenders}"

    def test_no_route_module_imports_argus_ai(self):
        # Stricter than "no provider SDK": the explanations routes must
        # not import argus.ai at all -- they read ExplanationRecord
        # directly, never reconstruct/validate/generate anything.
        offenders = []
        for module_name in _module_names(argus.api):
            module = importlib.import_module(module_name)
            source = inspect.getsource(module)
            dotted = _imported_dotted_prefixes(source)
            found = {m for m in dotted if m == "argus.ai" or m.startswith("argus.ai.")}
            if found:
                offenders.append((module_name, found))
        assert not offenders, f"argus.api module(s) import argus.ai: {offenders}"


class TestDoctorRouteIsTheOnlyNamedException:
    def test_doctor_route_module_actually_imports_the_docker_diagnostic_chain(self):
        # Guards against the exception above becoming stale/unnecessary:
        # if this ever stops being true, the carve-out in the tests
        # above should be removed, not left dangling.
        import argus.api.routes.doctor as doctor_route

        source = inspect.getsource(doctor_route)
        dotted = _imported_dotted_prefixes(source)
        assert any(m == "argus.doctor.checks" or m.startswith("argus.doctor") for m in dotted)

    def test_doctor_route_never_imports_a_provider_sdk_or_argus_ai(self):
        import argus.api.routes.doctor as doctor_route

        source = inspect.getsource(doctor_route)
        found_sdk = _imported_roots(source) & {"anthropic", "google"}
        dotted = _imported_dotted_prefixes(source)
        found_ai = {m for m in dotted if m == "argus.ai" or m.startswith("argus.ai.")}
        assert not found_sdk, f"doctor route imports a provider SDK: {found_sdk}"
        assert not found_ai, f"doctor route imports argus.ai: {found_ai}"


class TestPackageDiscovery:
    def test_api_package_has_the_expected_route_modules(self):
        names = {name.rsplit(".", 1)[-1] for name in _module_names(argus.api)}
        assert {
            "app", "config", "dependencies", "errors", "models",
            "system", "doctor", "applications", "incidents", "evidence", "bundles", "explanations", "events",
            "hosts", "agents",
        } <= names
