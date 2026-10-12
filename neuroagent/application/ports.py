"""Ports consumed by the application and worker layers."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

from neuroagent.application.repository_ports import (
    JobQueueRepositoryPort,
)
from neuroagent.application.repository_ports import (
    RepositoryPort as RepositoryPort,
)
from neuroagent.domain.fmri.qc import HeadMotionQcCriteria, HeadMotionQcReport
from neuroagent.execution.ports import (
    ExecutionResult as ExecutionResult,
)
from neuroagent.execution.ports import (
    MatlabExecutionCatalog,
    MockExecutionCatalog,
)


class WorkerHostRepositoryPort(
    JobQueueRepositoryPort, MatlabExecutionCatalog, MockExecutionCatalog, Protocol
):
    """Infrastructure surface composed by the Worker host, never by use cases."""

    def register_artifacts(
        self, project_id: str, run_id: str, artifacts: tuple[dict[str, Any], ...]
    ) -> None: ...


class ApplicationCapabilityRegistryPort(Protocol):
    """Resolve a named application capability from the composition root."""

    def resolve_capability(self, name: str) -> object: ...


class EventReader(Protocol):
    def list_events(self, run_id: str, after_event_id: int = 0) -> list[dict[str, Any]]: ...


class DatabaseLifecyclePort(Protocol):
    """Database liveness and lifecycle without exposing an ORM or SQL API."""

    def ping(self) -> None: ...

    def dispose(self) -> None: ...


class PathPolicyPort(Protocol):
    def validate_project_source_root(self, path: str | Path) -> Path: ...

    def validate_read_path(
        self,
        path: str | Path,
        *,
        project_roots: Iterable[str | Path],
        expect_directory: bool | None = None,
    ) -> Path: ...

    def validate_work_root(self, path: str | Path) -> Path: ...

    def storage_capacity(self, path: str | Path) -> tuple[int, int]: ...


class DatasetInspectorPort(Protocol):
    def inspect(
        self,
        source_path: Path,
        *,
        report_only: bool = False,
        selected_input_stage: str | None = None,
    ) -> dict[str, Any]: ...


class WorkspacePickerPort(Protocol):
    def pick_directory(self) -> str | None: ...


class WorkspaceFilePolicyPort(Protocol):
    root: Path

    def list_entries(self, **kwargs: Any) -> dict[str, Any]: ...
    def search(self, query: str, **kwargs: Any) -> dict[str, Any]: ...
    def read(self, relative: str, **kwargs: Any) -> Any: ...
    def read_image_metadata(self, relative: str) -> dict[str, Any]: ...
    def build_patch(self, edits: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]: ...
    def apply_patch(self, edits: list[dict[str, Any]], backup_root: Path) -> dict[str, Any]: ...
    def _check_file(self, relative: str) -> Path: ...
    def _relative(self, value: str, *, must_exist: bool = True) -> Path: ...


class DemographicsReaderPort(Protocol):
    def __call__(
        self,
        path: Path,
        *,
        subject_id_column: str,
        column_mapping: dict[str, str],
        encoding: str,
        manifest_subject_ids: set[str],
    ) -> dict[str, Any]: ...


class HeadMotionQcParserPort(Protocol):
    """Turn DPABI RealignParameter evidence into typed head-motion QC.

    The parser reads files from the filesystem, so the application layer depends
    on this port rather than importing the infrastructure adapter directly.
    """

    def __call__(
        self,
        realign_parameter_dir: Path,
        *,
        input_manifest_hash: str,
        subject_ids: tuple[str, ...],
        criteria: HeadMotionQcCriteria | None = None,
        expected_volume_counts: dict[str, int] | None = None,
    ) -> HeadMotionQcReport: ...


class SecretWriterPort(Protocol):
    """Write one validated secret through a controlled local adapter."""

    def write(self, secrets_file: Path, name: str, value: str) -> None: ...
