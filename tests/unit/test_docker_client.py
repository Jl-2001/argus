"""Tests for argus.collectors.docker_client.

Everything here uses a fake docker-py client injected via
``DockerClient(client=...)`` -- none of these tests require a real
Docker daemon to be running.
"""

from __future__ import annotations

import ast
import inspect

import docker.errors
import pytest

from argus.collectors import docker_client
from argus.collectors.docker_client import (
    ContainerVanishedError,
    DockerClient,
    DockerUnavailableError,
)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeContainer:
    def __init__(self, id: str, attrs: dict):
        self.id = id
        self.attrs = attrs


class FakeContainersAPI:
    def __init__(self, *, containers=None, list_error=None, get_results=None, get_error=None):
        self._containers = containers or []
        self._list_error = list_error
        self._get_results = get_results or {}
        self._get_error = get_error

    def list(self, all=False):
        if self._list_error is not None:
            raise self._list_error
        return self._containers

    def get(self, container_id):
        if self._get_error is not None:
            raise self._get_error
        try:
            return self._get_results[container_id]
        except KeyError:
            raise docker.errors.NotFound(f"no such container: {container_id}")


class FakeSDKClient:
    def __init__(self, **kwargs):
        self.containers = FakeContainersAPI(**kwargs)


# --------------------------------------------------------------------------
# list_containers
# --------------------------------------------------------------------------


class TestListContainers:
    def test_returns_ids_for_all_true(self):
        fake = FakeSDKClient(
            containers=[FakeContainer("id-1", {}), FakeContainer("id-2", {})]
        )
        client = DockerClient(client=fake)
        assert client.list_containers(all=True) == ["id-1", "id-2"]

    def test_passes_all_flag_through(self):
        seen = {}

        class RecordingContainersAPI(FakeContainersAPI):
            def list(self, all=False):
                seen["all"] = all
                return []

        fake = FakeSDKClient()
        fake.containers = RecordingContainersAPI()
        client = DockerClient(client=fake)

        client.list_containers(all=False)
        assert seen["all"] is False

    def test_docker_unavailable_during_list_raises_typed_error(self):
        fake = FakeSDKClient(list_error=docker.errors.DockerException("daemon gone"))
        client = DockerClient(client=fake)
        with pytest.raises(DockerUnavailableError):
            client.list_containers()


# --------------------------------------------------------------------------
# inspect_container
# --------------------------------------------------------------------------


class TestInspectContainer:
    def test_returns_fresh_attrs(self):
        fake = FakeSDKClient(get_results={"id-1": FakeContainer("id-1", {"State": {"Status": "running"}})})
        client = DockerClient(client=fake)
        result = client.inspect_container("id-1")
        assert result.id == "id-1"
        assert result.attrs == {"State": {"Status": "running"}}

    def test_not_found_raises_vanished_error(self):
        fake = FakeSDKClient(get_results={})
        client = DockerClient(client=fake)
        with pytest.raises(ContainerVanishedError):
            client.inspect_container("gone")

    def test_docker_unavailable_during_inspect_raises_typed_error(self):
        fake = FakeSDKClient(get_error=docker.errors.DockerException("daemon gone"))
        client = DockerClient(client=fake)
        with pytest.raises(DockerUnavailableError):
            client.inspect_container("id-1")


# --------------------------------------------------------------------------
# Construction / daemon unavailable at connect time
# --------------------------------------------------------------------------


class TestConstruction:
    def test_docker_exception_at_connect_time_becomes_typed_error(self, monkeypatch):
        def boom(**kwargs):
            raise docker.errors.DockerException("no daemon")

        monkeypatch.setattr(docker_client.docker, "from_env", boom)
        with pytest.raises(DockerUnavailableError):
            DockerClient()

    def test_os_error_at_connect_time_becomes_typed_error(self, monkeypatch):
        def boom(**kwargs):
            raise FileNotFoundError("no such socket")

        monkeypatch.setattr(docker_client.docker, "from_env", boom)
        with pytest.raises(DockerUnavailableError):
            DockerClient()

    def test_injected_client_skips_from_env(self, monkeypatch):
        def boom(**kwargs):
            raise AssertionError("from_env should not be called when a client is injected")

        monkeypatch.setattr(docker_client.docker, "from_env", boom)
        DockerClient(client=FakeSDKClient())  # must not raise


# --------------------------------------------------------------------------
# Read-only guard
# --------------------------------------------------------------------------

_MUTATING_CALL_PATTERNS = (
    ".start(",
    ".stop(",
    ".restart(",
    ".kill(",
    ".remove(",
    ".exec_run(",
    ".pause(",
    ".unpause(",
    ".rename(",
    ".update(",
    ".prune(",
    ".build(",
    ".pull(",
    ".push(",
    ".create(",
    ".run(",
    ".commit(",
)

_ESCAPE_HATCH_NAMES = {
    "raw_client",
    "get_client",
    "execute",
    "run_docker_command",
    "client",
    "sdk_client",
    "underlying_client",
}


class TestDockerClientIsReadOnly:
    def test_source_contains_no_mutating_docker_calls(self):
        source = inspect.getsource(docker_client)
        found = [pattern for pattern in _MUTATING_CALL_PATTERNS if pattern in source]
        assert not found, f"docker_client.py contains mutating call(s): {found}"

    def test_no_public_escape_hatch_method_or_attribute(self):
        tree = ast.parse(inspect.getsource(docker_client))
        class_def = next(
            node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "DockerClient"
        )
        public_names = {
            node.name
            for node in class_def.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not node.name.startswith("_")
        }
        leaked = public_names & _ESCAPE_HATCH_NAMES
        assert not leaked, f"DockerClient exposes an escape-hatch method: {leaked}"

    def test_no_way_to_reach_the_underlying_sdk_client_at_runtime(self):
        fake = FakeSDKClient(containers=[])
        client = DockerClient(client=fake)
        public_attrs = [name for name in vars(client) if not name.startswith("_")]
        assert public_attrs == [], f"DockerClient exposes public attribute(s): {public_attrs}"
        for escape_name in _ESCAPE_HATCH_NAMES:
            assert not hasattr(client, escape_name), f"DockerClient exposes {escape_name!r}"


# --------------------------------------------------------------------------
# Architecture guard (extends the pattern from Milestones 1-2)
# --------------------------------------------------------------------------

FORBIDDEN_IMPORT_ROOTS = {
    "sqlite3",
    "sqlalchemy",
    "anthropic",
    "openai",
    "langgraph",
    "fastapi",
}


def _imported_roots(source: str) -> set[str]:
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


class TestArchitectureGuard:
    def test_docker_client_has_no_persistence_or_ai_imports(self):
        source = inspect.getsource(docker_client)
        found = _imported_roots(source) & FORBIDDEN_IMPORT_ROOTS
        assert not found, f"docker_client.py imports forbidden module(s): {found}"
