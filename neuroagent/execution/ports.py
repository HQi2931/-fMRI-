"""Ports and results owned by the controlled execution boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: str
    output: dict[str, Any] = field(default_factory=dict)
    artifacts: tuple[dict[str, Any], ...] = ()
    error: str | None = None


class JobExecutor(Protocol):
    def execute(
        self,
        payload: dict[str, Any],
        *,
        is_cancelled: Callable[[], bool],
    ) -> ExecutionResult: ...


class ArtifactMetadata(Protocol):
    """Read-only artifact facts needed while compiling an execution command."""

    artifact_id: str
    project_id: str
    run_id: str
    relative_path: str
    checksum: str
    size_bytes: int
    provenance: dict[str, Any]


class MatlabJobCatalog(Protocol):
    """Capability-scoped catalog used by the MATLAB job compiler."""

    def get_artifact(self, artifact_id: str) -> ArtifactMetadata: ...

    # These application views are consumed structurally during migration. Keeping
    # the compiler on this narrow port prevents it from importing the aggregate port.
    def get_external_analysis_import(self, import_id: str) -> Any: ...

    def get_latest_external_demographics(self, import_id: str) -> Any: ...


class MatlabExecutionCatalog(MatlabJobCatalog, Protocol):
    """Read/write facts needed by the controlled MATLAB adapter."""

    def get_run(self, run_id: str) -> Any: ...

    def get_project(self, project_id: str) -> Any: ...

    def get_plan(self, plan_revision_id: str) -> Any: ...

    def get_dataset(self, dataset_id: str) -> Any: ...

    def get_manifest(self, manifest_id: str) -> Any: ...

    def get_manifest_content(self, manifest_id: str) -> dict[str, Any]: ...

    def get_approved_plan_approval(self, plan_revision_id: str) -> Any: ...

    def append_event(
        self,
        *,
        project_id: str | None,
        run_id: str | None,
        event_type: str,
        severity: str,
        payload: dict[str, object],
    ) -> None: ...


class MockExecutionCatalog(Protocol):
    """Read-only repository surface used by deterministic workflow execution."""

    def get_run(self, run_id: str) -> Any: ...

    def get_plan(self, plan_revision_id: str) -> Any: ...

    def get_approved_plan_approval(self, plan_revision_id: str) -> Any: ...

    def get_manifest(self, manifest_id: str) -> Any: ...

    def get_project(self, project_id: str) -> Any: ...
