"""Worker adapter for the controlled MATLAB executor."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

from neuroagent.application.environment_lock import (
    EnvironmentConfiguration,
    EnvironmentLockProvider,
)
from neuroagent.application.ports import ExecutionResult, RepositoryPort
from neuroagent.application.settings import Settings
from neuroagent.domain.fmri.artifacts import ArtifactKind, ArtifactLineage, MetricScaling
from neuroagent.domain.fmri.metrics import SmoothingTiming, TemporalFilterTiming
from neuroagent.domain.fmri.preprocessing import NormalizationTiming
from neuroagent.domain.fmri.statistics import CorrectionSpec, StatisticalDesignRevision
from neuroagent.execution.compiler import MatlabJobCompiler
from neuroagent.execution.matlab import ControlledMatlabExecutor, MatlabTemplateRenderer
from neuroagent.execution.models import (
    ArtifactPathBinding,
    MatlabEnvironment,
    MatlabJobSpec,
    MatlabJobStatus,
    PreprocessingJobPayload,
)
from neuroagent.execution.preprocessing import PreprocessingCompilation, compile_preprocessing
from neuroagent.infrastructure.filesystem.path_policy import PathPolicy
from neuroagent.skills.models import SkillPlan, stable_hash
from neuroagent.tools.input_staging import (
    InputFormat,
    StagingCopyOperation,
    StagingCopyPlan,
    StagingCopyTool,
)
from neuroagent.workflow.runtime import WorkflowFactory


class MatlabJobExecutor:
    """Compile server-owned payloads and run them through ControlledMatlabExecutor."""

    def __init__(
        self,
        repository: RepositoryPort,
        settings: Settings,
        *,
        environment_provider: EnvironmentLockProvider | None = None,
        workflow_factory: WorkflowFactory | None = None,
    ) -> None:
        renderer = MatlabTemplateRenderer(
            Path(__file__).resolve().parents[2] / "matlab" / "templates"
        )
        self._settings = settings
        self._repository = repository
        self._environment_provider = environment_provider
        self._compiler = MatlabJobCompiler(repository)
        self._renderer = renderer
        self._workflow_factory = workflow_factory

    def execute(
        self,
        payload: dict[str, Any],
        *,
        is_cancelled: Callable[[], bool],
    ) -> ExecutionResult:
        executor_type = payload.get("executor_type")
        if executor_type not in {"matlab_preprocessing", "matlab_statistics"}:
            return ExecutionResult(status="failed_terminal", error="invalid MATLAB executor type")
        if payload.get("real_execution_confirmed") is not True:
            return ExecutionResult(
                status="failed_terminal",
                error="real MATLAB execution requires per-run confirmation",
            )
        try:
            run_id = str(payload["run_id"])
            run = self._repository.get_run(run_id)
            project = self._repository.get_project(run.project_id)
            path_policy = PathPolicy(
                self._settings.allowed_source_roots, self._settings.allowed_work_root
            )
            work_root = path_policy.validate_work_root(project.work_root)
            work_root.mkdir(parents=True, exist_ok=True)
            if not self._settings.enable_real_execution:
                raise ValueError("real MATLAB execution is disabled")
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
            compilation = None
            preprocessing_workspace: Path | None = None
            if executor_type == "matlab_preprocessing":
                compilation = self._compile_preprocessing(payload)
                spec = compilation.spec
            else:
                design = StatisticalDesignRevision.model_validate(payload["statistical_design"])
                correction = _parse_correction(payload.get("correction"))
                spec = self._compiler.compile_statistics(
                    job_id=str(payload["job_id"]),
                    run_id=run_id,
                    plan_hash=str(payload["plan_hash"]),
                    approval_record_id=str(payload["approval_record_id"]),
                    input_manifest_hash=str(payload["input_manifest_hash"]),
                    design=design,
                    correction=correction,
                )
            attempts = (work_root / run_id / "attempts").resolve()
            if not _within(attempts, work_root):
                raise ValueError("attempt directory escaped project workspace")
            attempts.mkdir(parents=True, exist_ok=True)
            attempt = Path(tempfile.mkdtemp(prefix="attempt-", dir=attempts))
            spec = spec.model_copy(update={"run_id": attempt.name})
            if compilation is not None:
                compilation = replace(compilation, spec=spec)
                if payload.get("workspace_mode") == "in_place":
                    frozen = self._repository.get_plan(run.plan_revision_id)
                    plan = SkillPlan.model_validate(frozen.plan["skill_plan"])
                    dataset = self._repository.get_dataset(plan.dataset_ref)
                    manifest = self._repository.get_manifest(
                        str(frozen.plan["dataset_manifest_id"])
                    )
                    if manifest.profile.kind.value != "dpabi_ready":
                        raise ValueError("in-place execution requires a DPABI-ready workspace")
                    preprocessing_workspace = path_policy.validate_read_path(
                        dataset.source_path,
                        project_roots=project.source_roots,
                        expect_directory=True,
                    )
                    if compilation.staging_plan is None:
                        raise ValueError("in-place execution requires frozen source inputs")
                    self._verify_in_place_inputs(compilation.staging_plan, preprocessing_workspace)
                    stages = {
                        item.functional_files[0].replace("\\", "/").split("/", 1)[0]
                        for item in manifest.subjects
                    }
                    if len(stages) != 1:
                        raise ValueError("in-place DPABI inputs must use one functional stage")
                    compiled_payload = compilation.spec.payload
                    if not isinstance(compiled_payload, PreprocessingJobPayload):
                        raise ValueError("preprocessing compilation payload is invalid")
                    cfg = dict(compiled_payload.metric_projection.cfg)
                    cfg["StartingDirName"] = next(iter(stages))
                    mask_file = cfg.get("MaskFile")
                    if isinstance(mask_file, str) and mask_file != "Default":
                        cfg["MaskFile"] = (
                            PurePosixPath(".neuroagent") / attempt.name / mask_file
                        ).as_posix()
                    projection = compiled_payload.metric_projection.model_copy(
                        update={"cfg": cfg, "cfg_hash": stable_hash(cfg)}
                    )
                    in_place_payload = compiled_payload.model_copy(
                        update={"metric_projection": projection}
                    )
                    spec = compilation.spec.model_copy(update={"payload": in_place_payload})
                    compilation = replace(compilation, spec=spec)
            executor = ControlledMatlabExecutor(
                self._renderer,
                environment,
                attempts,
                allow_real_execution=self._settings.enable_real_execution,
                preprocessing_workspace=preprocessing_workspace,
            )
            rendered = executor.dry_run(spec).rendered
            self._stage_inputs(run_id, rendered.run_directory, spec)
            if preprocessing_workspace is not None:
                self._stage_in_place_mask(spec, rendered.run_directory, preprocessing_workspace)
            if (
                compilation is not None
                and compilation.staging_plan is not None
                and preprocessing_workspace is None
            ):
                frozen = self._repository.get_plan(run.plan_revision_id)
                plan = SkillPlan.model_validate(frozen.plan["skill_plan"])
                dataset = self._repository.get_dataset(plan.dataset_ref)
                source_root = path_policy.validate_read_path(
                    dataset.source_path,
                    project_roots=project.source_roots,
                    expect_directory=True,
                )
                StagingCopyTool().execute(
                    compilation.staging_plan,
                    source_root=source_root,
                    run_root=rendered.run_directory,
                )
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
        artifacts: tuple[dict[str, Any], ...] = tuple(
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
        if compilation is not None:
            try:
                artifacts = self._preprocessing_lineage(
                    artifacts, compilation, result.rendered.run_directory
                )
            except (ValueError, KeyError, OSError, TypeError) as exc:
                return ExecutionResult(
                    status="failed_terminal",
                    error=f"preprocessing evidence validation failed: {type(exc).__name__}",
                )
        for artifact in artifacts:
            artifact["relative_path"] = (
                PurePosixPath("attempts") / attempt.name / artifact["relative_path"]
            ).as_posix()
        return ExecutionResult(
            status="succeeded",
            output={
                "executor": "controlled_matlab",
                "job_hash": result.job_hash,
                "exit_code": result.exit_code,
            },
            artifacts=artifacts,
        )

    @staticmethod
    def _verify_in_place_inputs(plan: StagingCopyPlan, workspace: Path) -> None:
        root = workspace.resolve(strict=True)
        for operation in plan.operations:
            source = (root / operation.source_relative_path).resolve(strict=True)
            if not _within(source, root) or not source.is_file() or source.is_symlink():
                raise ValueError("frozen in-place input is unavailable or unsafe")
            if source.stat().st_size != operation.size_bytes:
                raise ValueError("frozen in-place input size changed")
            digest = hashlib.sha256()
            with source.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != operation.sha256:
                raise ValueError("frozen in-place input hash changed")

    @staticmethod
    def _stage_in_place_mask(spec: MatlabJobSpec, run_directory: Path, workspace: Path) -> None:
        if not isinstance(spec.payload, PreprocessingJobPayload):
            return
        mask_file = spec.payload.metric_projection.cfg.get("MaskFile")
        if not isinstance(mask_file, str) or mask_file == "Default":
            return
        bindings = [
            binding
            for binding in spec.artifact_bindings
            if binding.relative_path.startswith("staging/mask/")
        ]
        if len(bindings) != 1:
            raise ValueError("in-place metric execution requires one frozen mask")
        source = (run_directory / bindings[0].relative_path).resolve(strict=True)
        root = workspace.resolve(strict=True)
        destination = (root / mask_file).resolve()
        if not _within(destination, root):
            raise ValueError("in-place mask path escaped workspace")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    def _stage_inputs(self, run_id: str, target_root: Path, spec: MatlabJobSpec) -> None:
        run = self._repository.get_run(run_id)
        project = self._repository.get_project(run.project_id)
        project_root = Path(project.work_root).resolve(strict=True)
        for binding in spec.artifact_bindings:
            if binding.artifact_id.startswith("builtin:dpabi-v82-base-cfg:"):
                continue
            artifact = self._repository.get_artifact(binding.artifact_id)
            if artifact.project_id != run.project_id:
                raise ValueError("cross-project source artifact")
            operation = StagingCopyOperation(
                source_artifact_id=artifact.artifact_id,
                source_relative_path=(
                    PurePosixPath(artifact.run_id) / artifact.relative_path
                ).as_posix(),
                destination_relative_path=binding.relative_path,
                sha256=artifact.checksum,
                size_bytes=artifact.size_bytes,
            )
            StagingCopyTool().execute(
                StagingCopyPlan(
                    source_artifact_id=artifact.artifact_id,
                    input_manifest_hash=spec.input_manifest_hash,
                    input_format=InputFormat.NIFTI,
                    source_read_only=True,
                    operations=(operation,),
                    plan_hash=stable_hash(operation.model_dump(mode="json")),
                ),
                source_root=project_root,
                run_root=target_root,
            )

    def _compile_preprocessing(self, payload: dict[str, Any]) -> PreprocessingCompilation:
        run = self._repository.get_run(str(payload["run_id"]))
        frozen = self._repository.get_plan(run.plan_revision_id)
        approval = self._repository.get_approved_plan_approval(run.plan_revision_id)
        plan = SkillPlan.model_validate(frozen.plan["skill_plan"])
        if (
            payload.get("workflow_plan") != frozen.plan["skill_plan"]
            or payload.get("plan_hash") != frozen.plan_hash
            or payload.get("approval_record_id") != approval.approval_id
            or payload.get("input_manifest_hash") != frozen.manifest_hash
            or plan.project_id != run.project_id
        ):
            raise ValueError("preprocessing payload does not match frozen approval")
        if self._workflow_factory is None:
            raise ValueError("preprocessing WorkflowFactory is not configured")
        self._workflow_factory.from_approved_plan(
            plan,
            approval_plan_hash=frozen.plan_hash,
            current_environment_hash=str(payload["environment_hash"]),
        )
        manifest_id = str(frozen.plan["dataset_manifest_id"])
        manifest = self._repository.get_manifest(manifest_id)
        parameters = dict(plan.resolved_parameters)
        masks = {
            str(parameters[name]["mask_artifact_id"])
            for name in ("alff_falff", "reho")
            if name in parameters
        }
        if len(masks) > 1:
            raise ValueError("combined metrics require one frozen mask")
        mask_binding = None
        if masks:
            mask_id = masks.pop()
            mask = self._repository.get_artifact(mask_id)
            lineage = ArtifactLineage.model_validate(mask.provenance.get("lineage"))
            if (
                mask.project_id != run.project_id
                or lineage.kind is not ArtifactKind.BRAIN_MASK
                or not lineage.metadata_verified
                or not lineage.metadata_evidence_hash
                or lineage.subject_manifest_hash != plan.input_manifest_hash
            ):
                raise ValueError("metric mask does not match project and manifest")
            suffix = ".nii.gz" if mask.relative_path.endswith(".nii.gz") else ".nii"
            mask_binding = ArtifactPathBinding(
                artifact_id=mask_id, relative_path=f"staging/mask/mask{suffix}", read_only=True
            )
        input_binding = None
        if "preprocessing" not in parameters:
            source = self._repository.get_artifact(plan.input_artifact_id)
            lineage = ArtifactLineage.model_validate(source.provenance.get("lineage"))
            if (
                source.project_id != run.project_id
                or not lineage.metadata_verified
                or lineage.kind is not ArtifactKind.FUNCTIONAL_TIMESERIES
                or lineage.temporally_filtered
                or lineage.spatially_smoothed
                or lineage.subject_manifest_hash != plan.input_manifest_hash
                or len(manifest.subjects) != 1
                or lineage.subject_id != manifest.subjects[0].subject_id
            ):
                raise ValueError("metric input is not the frozen verified checkpoint")
            suffix = ".nii.gz" if source.relative_path.endswith(".nii.gz") else ".nii"
            input_binding = ArtifactPathBinding(
                artifact_id=source.artifact_id,
                relative_path=f"staging/FunImg/{lineage.subject_id}/input{suffix}",
                read_only=True,
            )
        return compile_preprocessing(
            plan,
            manifest,
            self._repository.get_manifest_content(manifest_id),
            job_id=str(payload["job_id"]),
            run_id=run.run_id,
            approval_record_id=approval.approval_id,
            mask_binding=mask_binding,
            input_binding=input_binding,
        )

    def _preprocessing_lineage(
        self,
        artifacts: tuple[dict[str, Any], ...],
        compilation: PreprocessingCompilation,
        root: Path,
    ) -> tuple[dict[str, Any], ...]:
        evidence = json.loads((root / "output/preprocessing-evidence.json").read_text())
        rows = evidence["outputs"]
        if isinstance(rows, dict):
            rows = [rows]
        by_path = {row["relative_path"]: row for row in rows}
        assert isinstance(compilation.spec.payload, PreprocessingJobPayload)
        outputs = {row.relative_path: row for row in compilation.spec.payload.outputs}
        if set(by_path) != set(outputs) or len(rows) != len(outputs):
            raise ValueError("preprocessing output identity mismatch")
        parameters = compilation.parameters
        for artifact in artifacts:
            path = artifact["relative_path"]
            if path not in outputs:
                continue
            output = outputs[path]
            row = by_path[path]
            if row["subject_id"] != output.subject_id or row["metric"] != output.metric:
                raise ValueError("preprocessing subject mismatch")
            metadata = row["metadata"]
            metric = output.metric
            mask_id = None
            mask_grid = None
            grid = stable_hash({"dim": metadata["dimensions"], "affine": metadata["affine"]})
            metric_parameters = (
                compilation.reho if metric == "reho" else compilation.alff or compilation.reho
            )
            if metric_parameters is not None:
                mask_id = metric_parameters.mask_artifact_id
                # The fixed template compares actual output and staged mask grids.
                # Persist the grid observed in this run, never a producer's opaque label.
                mask_grid = grid
            filtered_band = None
            smoothing = None
            if metric == "timeseries":
                if parameters is None:
                    pass  # Metric-only checkpoints retain their unsmoothed input.
                elif compilation.alff is None and compilation.reho is None:
                    filtered_band = parameters.temporal_filter.frequency_band
                    if parameters.smoothing.timing is SmoothingTiming.ON_FUNCTIONAL_DATA:
                        smoothing = parameters.smoothing.fwhm_mm
                elif parameters.temporal_filter.timing is TemporalFilterTiming.BEFORE_NORMALIZE:
                    filtered_band = parameters.temporal_filter.frequency_band
            elif metric == "reho" and compilation.reho is not None:
                filtered_band = compilation.reho.temporal_filter_band
                smoothing = (
                    compilation.reho.smooth_reho_fwhm_mm
                    or compilation.reho.global_result_smoothing_fwhm_mm
                )
            elif compilation.alff is not None:
                smoothing = compilation.alff.result_smoothing_fwhm_mm
            kind = {
                "timeseries": ArtifactKind.FUNCTIONAL_TIMESERIES,
                "alff": ArtifactKind.ALFF_MAP,
                "falff": ArtifactKind.FALFF_MAP,
                "reho": ArtifactKind.REHO_MAP,
            }[metric]
            space = "native"
            if (
                parameters is not None
                and parameters.normalization.timing is NormalizationTiming.ON_FUNCTIONAL_DATA
            ):
                space = "MNI"
            source_scrubbed = False
            source_producer_hash = None
            if parameters is None:
                source_binding = next(
                    binding
                    for binding in compilation.spec.artifact_bindings
                    if binding.relative_path.startswith("staging/FunImg/")
                )
                source = self._repository.get_artifact(source_binding.artifact_id)
                source_lineage = ArtifactLineage.model_validate(source.provenance["lineage"])
                space = source_lineage.space
                source_scrubbed = source_lineage.scrubbed
                source_producer_hash = source_lineage.producer_step_hash
            step_id = {
                "timeseries": "verify_preprocessed_metadata",
                "alff": "calculate_alff_falff",
                "falff": "calculate_alff_falff",
                "reho": "calculate_reho",
            }[metric]
            step_hashes = dict(compilation.producer_step_hashes)
            producer_hash = (
                source_producer_hash
                if metric == "timeseries" and source_producer_hash is not None
                else step_hashes.get(step_id)
            )
            if producer_hash is None:
                raise ValueError(f"frozen plan has no producer step for {metric}")
            lineage = ArtifactLineage(
                artifact_id="pending-registration",
                kind=kind,
                subject_id=output.subject_id,
                session_id=next(
                    item.session_id
                    for item in compilation.subjects
                    if item.subject_id == output.subject_id
                ),
                metric_scaling=MetricScaling(output.scaling) if output.scaling else None,
                metadata_verified=True,
                tr_seconds=row["tr_seconds"],
                volume_count=row["volume_count"],
                metadata_evidence_hash=stable_hash(
                    {
                        "row": row,
                        "sha256": artifact["checksum"],
                        "job_hash": compilation.spec.job_hash,
                    }
                ),
                subject_manifest_hash=compilation.spec.input_manifest_hash,
                space=space,
                grid_signature=grid,
                voxel_size_mm=metadata["voxel_size_mm"],
                mask_artifact_id=mask_id,
                mask_grid_signature=mask_grid,
                temporally_filtered=filtered_band is not None,
                frequency_band=filtered_band,
                spatially_smoothed=smoothing is not None,
                smoothing_fwhm_mm=smoothing,
                scrubbed=source_scrubbed
                or bool(
                    parameters
                    and parameters.scrubbing.enabled
                    and metric != "alff"
                    and metric != "falff"
                    and (
                        metric == "reho" or (compilation.reho is None and compilation.alff is None)
                    )
                ),
                producer_step_hash=producer_hash,
            )
            artifact["provenance"]["lineage"] = lineage.model_dump(mode="json")
        return artifacts


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
