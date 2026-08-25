"""Milestone 16 -- import-boundary and mutation-surface guard for
`argus.agent`, mirroring `tests/unit/test_ai_architecture_guard.py`'s
own AST-based approach:

1. No `argus.agent` module imports an AI provider SDK (`anthropic`,
   `google`), the central incident engine (`argus.incidents`,
   `argus.ingestion`), the dashboard/API (`argus.api`), or a
   shell-execution helper (`subprocess`, `os.system`).
2. `argus.agent` never calls a Docker *mutation* method -- no
   `.start(`, `.stop(`, `.restart(`, `.kill(`, `.remove(`, `.exec_run(`,
   `.create(` anywhere in its own source.
3. The wire payload this package builds (`AgentSnapshot`/
   `EvidenceCandidateWire`, via `to_dict()`) never contains raw Docker
   env, mount, or label data, and never a token/credential.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
from datetime import datetime, timezone

import argus.agent
from argus.agent.protocol import PROTOCOL_VERSION, AgentSnapshot
from argus.domain.models import Application, Container, DockerState, HealthStatus, Observation, PortBinding, Protocol, Service

_FORBIDDEN_ROOTS = {"anthropic", "google", "subprocess"}
_FORBIDDEN_DOTTED_PREFIXES = ("argus.incidents", "argus.ingestion", "argus.api", "argus.ai")
_MUTATING_DOCKER_CALL_PATTERN = (
    r"\.(start|stop|restart|kill|remove|exec_run|create|update|build|pull|push|prune)\s*\("
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _module_names(package) -> list[str]:
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


def _imported_dotted(source: str) -> set[str]:
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


class TestAgentNeverImportsForbiddenDependencies:
    def test_no_agent_module_imports_a_provider_sdk_or_subprocess(self):
        offenders = []
        for module_name in _module_names(argus.agent):
            module = importlib.import_module(module_name)
            source = inspect.getsource(module)
            found = _imported_roots(source) & _FORBIDDEN_ROOTS
            if found:
                offenders.append((module_name, found))
        assert not offenders, f"argus.agent module(s) import a forbidden dependency: {offenders}"

    def test_no_agent_module_imports_incidents_ingestion_api_or_ai(self):
        offenders = []
        for module_name in _module_names(argus.agent):
            module = importlib.import_module(module_name)
            source = inspect.getsource(module)
            dotted = _imported_dotted(source)
            found = {
                m for m in dotted
                if any(m == prefix or m.startswith(prefix + ".") for prefix in _FORBIDDEN_DOTTED_PREFIXES)
            }
            if found:
                offenders.append((module_name, found))
        assert not offenders, f"argus.agent module(s) import a forbidden argus subsystem: {offenders}"

    def test_no_agent_module_uses_os_system(self):
        offenders = []
        for module_name in _module_names(argus.agent):
            module = importlib.import_module(module_name)
            source = inspect.getsource(module)
            if "os.system(" in source or "os.popen(" in source:
                offenders.append(module_name)
        assert not offenders, f"argus.agent module(s) shell out via os.system/os.popen: {offenders}"


class TestAgentNeverCallsAMutatingDockerMethod:
    def test_no_agent_module_source_contains_a_mutating_docker_call(self):
        import re

        pattern = re.compile(_MUTATING_DOCKER_CALL_PATTERN)
        offenders = []
        for module_name in _module_names(argus.agent):
            module = importlib.import_module(module_name)
            source = inspect.getsource(module)
            match = pattern.search(source)
            if match:
                offenders.append((module_name, match.group(0)))
        assert not offenders, f"argus.agent module(s) call a mutating Docker method: {offenders}"


class TestAgentSnapshotWireContainsNoSensitiveData:
    def _make_snapshot(self) -> AgentSnapshot:
        container = Container(
            container_id="c" * 64, name="api-1", image="cnstrct/api:latest",
            compose_project="cnstrct", compose_service="api", first_seen_at=T0, last_seen_at=T0,
        )
        observation = Observation(
            container_ref=container, observed_at=T0, docker_state=DockerState.RUNNING, docker_health=None,
            restart_count=0, exit_code=None, started_at=T0, finished_at=None,
            ports=(PortBinding(container_port=8080, protocol=Protocol.TCP, host_ip="0.0.0.0", host_port=8080),),
            labels={"com.docker.compose.project": "cnstrct"},
            derived_status=HealthStatus.HEALTHY, derived_detail=None,
        )
        service = Service(
            application_key="cnstrct", compose_service="api", containers=(container,),
            derived_status=HealthStatus.HEALTHY,
        )
        application = Application(
            key="cnstrct", name="CNSTRCT", is_standalone=False, services=(service,),
            derived_status=HealthStatus.HEALTHY,
        )
        return AgentSnapshot(
            protocol_version=PROTOCOL_VERSION, agent_id="agent-1", host_key="dell", generated_at=T0,
            agent_version="0.1.0", applications=(application,), observations=(observation,),
            evidence_candidates=(),
        )

    def test_wire_payload_never_contains_env_mounts_or_raw_labels(self):
        payload = self._make_snapshot().to_dict()
        text = str(payload)
        assert "Config.Env" not in text
        assert "Env" not in payload["applications"][0]  # Application.to_dict has no Env key at all
        assert "Mounts" not in text
        assert "HostConfig" not in text

    def test_wire_payload_never_contains_a_token_or_credential_field(self):
        payload = self._make_snapshot().to_dict()
        text = str(payload).lower()
        for forbidden in ("token", "password", "api_key", "apikey", "secret", "authorization"):
            assert forbidden not in text, f"{forbidden!r} unexpectedly present in snapshot wire payload"

    def test_wire_payload_never_contains_a_docker_socket_path(self):
        payload = self._make_snapshot().to_dict()
        text = str(payload)
        assert "/var/run/docker.sock" not in text
        assert "unix://" not in text

    def test_only_allowlisted_labels_reach_the_wire(self):
        # Observation.labels is already the allowlisted-only mapping by
        # construction (Milestone 3) -- this asserts the wire payload
        # carries exactly what was put on the domain object, adding no
        # new raw label data of its own.
        payload = self._make_snapshot().to_dict()
        wire_labels = payload["observations"][0]["labels"]
        assert wire_labels == {"com.docker.compose.project": "cnstrct"}
