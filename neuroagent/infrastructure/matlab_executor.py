"""Worker adapter for the controlled MATLAB executor."""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from neuroagent.application.environment_lock import (
    EnvironmentConfiguration,
    EnvironmentLockProvider,
)
from neuroagent.application.ports import ExecutionResult, RepositoryPort
from neuroagent.application.settings import Settings
from neuroagent.domain.fmri.statistics import CorrectionSpec, StatisticalDesignRevision
from neuroagent.execution.compiler import MatlabJobCompiler
from neuroagent.execution.matlab import ControlledMatlabExecutor, MatlabTemplateRenderer
from neuroagent.execution.models import MatlabEnvironment, MatlabJobSpec, MatlabJobStatus


class MatlabJobExecutor:
    """Compile server-owned payloads and run them through ControlledMatlabExecutor."""

    def __init__(
        self,
        repository: RepositoryPort,
        settings: Settings,
        *,
        environment_provider: EnvironmentLockProvider | None = None,
    ) -> None:
        renderer = MatlabTemplateRenderer(
            Path(__file__).resolve().parents[2] / "matlab" / "templates"
        )
        self._settings = settings
        self._repository = repository
        self._environment_provider = environment_provider
        self._compiler = MatlabJobCompiler(repository)
        self._renderer = renderer

    def execute(
        self,
        payload: dict[str, Any],
        *,
        is_cancelled: Callable[[], bool],
    ) -> ExecutionResult:
        executor_type = payload.get("executor_type")
        if executor_type not in {"matlab_preprocessing", "matlab_statistics"}:
            return ExecutionResult(status="failed_terminal", error="invalid MATLAB executor type")
        if executor_type != "matlab_statistics":
            return ExecutionResult(
                status="failed_terminal",
                error="MATLAB preprocessing compiler requires a typed SkillPlan payload",
            )
        try:
            current = (
                self._environment_provider.current()
                if self._environment_provider is not None
                else None
            )
            if current is not None and not current.probe.ready:
                return ExecutionResult(
                    status="failed_terminal", error="MATLAB environment is not ready"
                )
            if current is not None and current.probe.environment_hash != str(
                payload.get("environment_hash")
            ):
                return ExecutionResult(
                    status="failed_terminal", error="MATLAB environment lock drifted"
                )
            configuration = (
                current.configuration
                if current is not None and current.configuration is not None
                else _settings_configuration(self._settings)
            )
            if (
                configuration.matlab_executable is None
                or configuration.spm_dir is None
                or configuration.dpabi_dir is None
            ):
                return ExecutionResult(
                    status="failed_terminal", error="MATLAB environment is not configured"
                )
            environment = MatlabEnvironment(
                matlab_executable=configuration.matlab_executable,
                matlab_root=configuration.matlab_executable.parent,
                spm_path=configuration.spm_dir,
                dpabi_path=configuration.dpabi_dir,
                matlab_version=configuration.matlab_version,
                spm_version=configuration.spm_version,
                dpabi_version=configuration.dpabi_version,
            )
            executor = ControlledMatlabExecutor(
                self._renderer,
                environment,
                self._settings.allowed_work_root,
                allow_real_execution=self._settings.enable_real_execution,
            )
            run_id = str(payload["run_id"])
            design = StatisticalDesignRevision.model_validate(payload["statistical_design"])
            correction_payload = payload.get("correction")
            correction = _parse_correction(correction_payload)
            spec = self._compiler.compile_statistics(
                job_id=str(payload["job_id"]),
                run_id=run_id,
                plan_hash=str(payload["plan_hash"]),
                approval_record_id=str(payload["approval_record_id"]),
                input_manifest_hash=str(payload["input_manifest_hash"]),
                design=design,
                correction=correction,
            )
            rendered = executor.dry_run(spec).rendered
            self._stage_inputs(run_id, rendered.run_directory, spec)
            result = executor.execute(spec, is_cancelled=is_cancelled)
        except Exception as exc:
            return ExecutionResult(
                status="failed_terminal",
                error=f"MATLAB job preparation failed: {type(exc).__name__}",
            )
        status = {
            MatlabJobStatus.SUCCEEDED: "succeeded",
            MatlabJobStatus.CANCELLED: "cancelled",
            MatlabJobStatus.TIMED_OUT: "timed_out",
            MatlabJobStatus.FAILED: "failed_retryable",
            MatlabJobStatus.DRY_RUN: "failed_terminal",
        }[result.status]
        if status != "succeeded":
            return ExecutionResult(status=status, error=result.stderr or "MATLAB execution failed")
        artifacts = tuple(
            {
                "artifact_type": artifact.artifact_type,
                "relative_path": artifact.relative_path,
                "checksum": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "provenance": {
                    "executor": "controlled_matlab",
                    "job_hash": result.job_hash,
                    "plan_hash": spec.plan_hash,
                },
            }
            for artifact in result.registered_artifacts
        )
        return ExecutionResult(
            status="succeeded",
            output={
                "executor": "controlled_matlab",
                "job_hash": result.job_hash,
                "exit_code": result.exit_code,
            },
            artifacts=artifacts,
        )

    def _stage_inputs(self, run_id: str, target_root: Path, spec: MatlabJobSpec) -> None:
        run = self._repository.get_run(run_id)
        project = self._repository.get_project(run.project_id)
        project_root = Path(project.work_root).resolve(strict=True)
        for binding in spec.artifact_bindings:
            artifact = self._repository.get_artifact(binding.artifact_id)
            source = (project_root / artifact.run_id / artifact.relative_path).resolve(strict=True)
            if not _within(source, project_root) or not source.is_file():
                raise ValueError("bound source artifact is unavailable")
            digest = _sha256(source)
            if digest != artifact.checksum:
                raise ValueError("bound source artifact checksum changed")
            destination = (target_root / PurePosixPath(binding.relative_path)).resolve()
            if not _within(destination, target_root):
                raise ValueError("staged artifact escaped the run directory")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def _parse_correction(value: object) -> CorrectionSpec | None:
    if value is None:
        return None
    from neuroagent.domain.fmri.statistics import FdrCorrection, GrfCorrection

    if not isinstance(value, dict):
        raise ValueError("correction payload must be an object")
    if value.get("method") == "fdr":
        return FdrCorrection.model_validate(value)
    if value.get("method") == "grf":
        return GrfCorrection.model_validate(value)
    raise ValueError("unsupported correction method")


def _settings_configuration(settings: Settings) -> EnvironmentConfiguration:
    return EnvironmentConfiguration(
        matlab_executable=settings.matlab_executable,
        spm_dir=settings.spm_dir,
        dpabi_dir=settings.dpabi_dir,
        matlab_version=settings.matlab_version,
        spm_version=settings.spm_version,
        dpabi_version=settings.dpabi_version,
        adapter_version=settings.adapter_version,
    )


def _within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
