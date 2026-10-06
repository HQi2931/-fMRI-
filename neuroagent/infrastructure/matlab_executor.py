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
from neuroagent.domain.fmri.dpabi_stages import stage_rule, verified_source_version
from neuroagent.domain.fmri.metrics import MetricMaskSource, SmoothingTiming, TemporalFilterTiming
from neuroagent.domain.fmri.preprocessing import NormalizationTiming
from neuroagent.domain.fmri.qc import HeadMotionQcCriteria
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
from neuroagent.execution.preprocessing import (
    PreprocessingCompilation,
    compile_preprocessing,
)
from neuroagent.infrastructure.filesystem.head_motion_qc import parse_dpabi_head_motion_qc
from neuroagent.infrastructure.filesystem.path_policy import PathPolicy
from neuroagent.infrastructure.filesystem.t1_artifacts import validate_t1_artifact
from neuroagent.infrastructure.filesystem.t1_geometry import verify_t1_geometry
from neuroagent.skills.models import SkillPlan, stable_hash
from neuroagent.tools.input_staging import (
    InputFormat,
    StagingCopyOperation,
    StagingCopyPlan,
    StagingCopyTool,
)
from neuroagent.workflow.runtime import WorkflowFactory


def _apply_verified_stage_resume(cfg: dict[str, Any], stage: str) -> dict[str, Any]:
    """Disable DPABI steps already represented by a verified input checkpoint."""

    rule = stage_rule(stage)
    if rule is None:
        raise ValueError("unknown DPABI input stage")
    updated = dict(cfg)
    fields = {
        "slice_timing": "IsSliceTiming",
        "realignment": "IsRealign",
        "nuisance": "IsCovremove",
        "filter": "IsFilter",
        "normalization": "IsNormalize",
        "smoothing": "IsSmooth",
        "detrend": "IsDetrend",
        "scrubbing": "IsScrubbing",
    }
    for step in rule.completed_steps:
        field = fields.get(step)
        if field is not None:
            updated[field] = 0
    # DPARSFA mutates the selected input for dummy-scan removal.  A verified
    # checkpoint must never be temporally shortened a second time.
    if rule.completed_steps:
        updated["RemoveFirstTimePoints"] = 0
    return updated


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
        if payload.get("operation") == "t1_preparation":
            return ExecutionResult(
                status="failed_terminal", error="standalone T1 preparation is no longer supported"
            )
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
                assert isinstance(spec.payload, PreprocessingJobPayload)
                compilation = replace(compilation, spec=spec)
                if payload.get("workspace_mode") == "in_place":
                    if spec.payload.t1_preparation is not None:
                        raise ValueError("T1 stages require isolated staging")
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
                    # The selected checkpoint is part of the frozen manifest.
                    # Never recover the old implicit "single directory" behavior
                    # here: a plan created without explicit user confirmation must
                    # fail closed before MATLAB is launched.
                    stage = manifest.selected_input_stage
                    if not stage or stage_rule(stage) is None:
                        raise ValueError(
                            "in-place DPABI inputs require an explicitly confirmed functional stage"
                        )
                    selected_rule = stage_rule(stage)
                    if (
                        selected_rule is not None
                        and selected_rule.completed_steps
                        and not verified_source_version(configuration.dpabi_version)
                    ):
                        raise ValueError(
                            "intermediate DPABI stage resume requires the verified "
                            "DPABI V9.0_250415 source"
                        )
                    manifest_content = self._repository.get_manifest_content(
                        str(frozen.plan["dataset_manifest_id"])
                    )
                    self._verify_manifest_file_hashes(manifest_content, preprocessing_workspace)
                    compiled_payload = compilation.spec.payload
                    if not isinstance(compiled_payload, PreprocessingJobPayload):
                        raise ValueError("preprocessing compilation payload is invalid")
                    cfg = dict(compiled_payload.metric_projection.cfg)
                    downstream_needs_realign = bool(
                        any(
                            int(cfg.get(field, 0) or 0)
                            for field in (
                                "IsCovremove",
                                "IsFilter",
                                "IsNormalize",
                                "IsSmooth",
                                "IsDetrend",
                                "IsScrubbing",
                            )
                        )
                    )
                    cfg["StartingDirName"] = stage
                    cfg = _apply_verified_stage_resume(cfg, stage)
                    predicted_stage = _predict_dpabi_stage(stage, cfg)
                    if (
                        predicted_stage != stage
                        and (preprocessing_workspace / predicted_stage).exists()
                    ):
                        raise ValueError(f"DPABI output stage already exists: {predicted_stage}")
                    if (cfg.get("IsCalALFF", 0) or cfg.get("IsCalReHo", 0)) and (
                        preprocessing_workspace / "Results"
                    ).exists():
                        raise ValueError("DPABI Results directory already exists")
                    needs_realign_evidence = (
                        selected_rule is not None
                        and selected_rule.requires_realign_parameters
                        and (downstream_needs_realign or str(run.qc_mode or "require") != "skip")
                    )
                    if (
                        needs_realign_evidence
                        and not (preprocessing_workspace / "RealignParameter").is_dir()
                    ):
                        raise ValueError(
                            "selected DPABI checkpoint requires RealignParameter for "
                            "the requested downstream operation or QC"
                        )
                    mask_file = cfg.get("MaskFile")
                    if isinstance(mask_file, str) and mask_file not in {
                        "Default",
                        "__DPABI_AUTO_MASK__",
                    }:
                        cfg["MaskFile"] = (
                            PurePosixPath(".neuroagent") / attempt.name / mask_file
                        ).as_posix()
                    projection = compiled_payload.metric_projection.model_copy(
                        update={"cfg": cfg, "cfg_hash": stable_hash(cfg)}
                    )
                    in_place_payload = compiled_payload.model_copy(
                        update={
                            "metric_projection": projection,
                            # The verified checkpoint may disable Realign even
                            # when the generic plan requested it.  Keep the
                            # payload's explicit base-Cfg allow-list aligned
                            # with the effective frozen configuration.
                            "base_cfg_allowed_fields": (
                                ("Realign",) if cfg.get("IsRealign") else ()
                            ),
                        }
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
            assert isinstance(compilation.spec.payload, PreprocessingJobPayload)
            try:
                artifacts = self._preprocessing_lineage(
                    artifacts, compilation, result.rendered.run_directory
                )
                if (
                    str(run.qc_mode or "require") != "skip"
                ):
                    try:
                        artifacts = (
                            *artifacts,
                            *self._head_motion_qc_artifacts(
                                compilation,
                                result.rendered.run_directory,
                                preprocessing_workspace=preprocessing_workspace,
                                job_hash=result.job_hash,
                            ),
                        )
                    except (ValueError, KeyError, OSError, TypeError) as exc:
                        artifacts = (
                            *artifacts,
                            *self._head_motion_qc_failure_artifact(
                                result.rendered.run_directory,
                                job_hash=result.job_hash,
                                plan_hash=compilation.spec.plan_hash,
                                error_type=type(exc).__name__,
                            ),
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
    def _head_motion_qc_artifacts(
        compilation: PreprocessingCompilation,
        root: Path,
        *,
        preprocessing_workspace: Path | None,
        job_hash: str,
    ) -> tuple[dict[str, Any], ...]:
        if not isinstance(compilation.spec.payload, PreprocessingJobPayload):
            return ()
        cfg = compilation.spec.payload.metric_projection.cfg
        # Read the effective compiled DPABI flags.  In-place stage resume can
        # disable IsRealign even when the generic parameter draft enabled it;
        # QC provenance must describe what this run actually executed.
        current_realign = bool(cfg.get("IsRealign", 0))
        selected_stage = cfg.get("StartingDirName")
        selected_rule = stage_rule(selected_stage) if isinstance(selected_stage, str) else None
        prior_realign = bool(
            selected_rule is not None and "realignment" in selected_rule.completed_steps
        )
        if not current_realign and not prior_realign:
            return ()
        qc_source = "current_execution" if current_realign else "prior_checkpoint"
        evidence = json.loads((root / "output/preprocessing-evidence.json").read_text())
        rows = evidence["outputs"]
        if isinstance(rows, dict):
            rows = [rows]
        expected_counts = {
            str(row["subject_id"]): int(row["volume_count"])
            for row in rows
            if row.get("metric") == "timeseries"
        }
        realign_root = (
            preprocessing_workspace if preprocessing_workspace is not None else root / "staging"
        ) / "RealignParameter"
        report = parse_dpabi_head_motion_qc(
            realign_root,
            input_manifest_hash=compilation.spec.input_manifest_hash,
            subject_ids=tuple(subject.subject_id for subject in compilation.subjects),
            criteria=HeadMotionQcCriteria(),
            expected_volume_counts=expected_counts,
        )
        report_payload = report.model_dump(mode="json")
        report_json = (
            json.dumps(report_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        )
        relative_path = "output/qc/head-motion-qc.json"
        output_path = root / relative_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = report_json.encode("utf-8")
        output_path.write_bytes(encoded)
        digest = hashlib.sha256(encoded).hexdigest()
        return (
            {
                "artifact_type": "qc.head_motion_report",
                "relative_path": relative_path,
                "checksum": digest,
                "size_bytes": len(encoded),
                "provenance": {
                    "executor": "controlled_matlab",
                    "job_hash": job_hash,
                    "plan_hash": compilation.spec.plan_hash,
                    "source": qc_source,
                    "head_motion_qc": report_payload,
                },
            },
        )

    @staticmethod
    def _head_motion_qc_failure_artifact(
        root: Path,
        *,
        job_hash: str,
        plan_hash: str,
        error_type: str,
    ) -> tuple[dict[str, Any], ...]:
        payload = {
            "status": "failed",
            "reason": "head_motion_qc_parse_failed",
            "error_type": error_type,
        }
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        relative_path = "output/qc/head-motion-qc-failure.json"
        output_path = root / relative_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = text.encode("utf-8")
        output_path.write_bytes(encoded)
        return (
            {
                "artifact_type": "qc.head_motion_failure",
                "relative_path": relative_path,
                "checksum": hashlib.sha256(encoded).hexdigest(),
                "size_bytes": len(encoded),
                "provenance": {
                    "executor": "controlled_matlab",
                    "job_hash": job_hash,
                    "plan_hash": plan_hash,
                    "head_motion_qc_failure": payload,
                },
            },
        )

    @staticmethod
    def _verify_manifest_file_hashes(content: dict[str, Any], workspace: Path) -> None:
        """Recheck the immutable manifest, including DPABI companion evidence."""

        hashes = content.get("file_hashes")
        sizes = content.get("file_sizes")
        if not isinstance(hashes, dict) or not isinstance(sizes, dict):
            raise ValueError("frozen manifest is missing file hash evidence")
        root = workspace.resolve(strict=True)
        if not root.is_dir() or workspace.is_symlink():
            raise ValueError("in-place workspace is unavailable or unsafe")
        for relative, expected_hash in hashes.items():
            if not isinstance(relative, str) or not isinstance(expected_hash, str):
                raise ValueError("frozen manifest contains invalid file hash evidence")
            raw_candidate = root / PurePosixPath(relative)
            if raw_candidate.is_symlink() or _is_junction(raw_candidate):
                raise ValueError("frozen manifest file is unavailable or unsafe")
            candidate = raw_candidate.resolve(strict=True)
            if not _within(candidate, root) or not candidate.is_file():
                raise ValueError("frozen manifest file is unavailable or unsafe")
            expected_size = sizes.get(relative)
            if not isinstance(expected_size, int) or candidate.stat().st_size != expected_size:
                raise ValueError("frozen manifest file size changed")
            digest = hashlib.sha256()
            with candidate.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != expected_hash:
                raise ValueError("frozen manifest file hash changed")

    @staticmethod
    def _stage_in_place_mask(spec: MatlabJobSpec, run_directory: Path, workspace: Path) -> None:
        if not isinstance(spec.payload, PreprocessingJobPayload):
            return
        mask_file = spec.payload.metric_projection.cfg.get("MaskFile")
        if not isinstance(mask_file, str) or mask_file in {
            "Default",
            "__DPABI_AUTO_MASK__",
        }:
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
        dataset = self._repository.get_dataset(plan.dataset_ref)
        if dataset.current_manifest_id != manifest_id:
            raise ValueError("dataset manifest revision changed after plan approval")
        parameters = dict(plan.resolved_parameters)
        masks = {
            str(parameters[name]["mask_artifact_id"])
            for name in ("alff_falff", "reho")
            if name in parameters and parameters[name].get("mask_artifact_id")
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

    def _t1_preparation_lineage(
        self,
        artifacts: tuple[dict[str, Any], ...],
        compilation: PreprocessingCompilation,
        root: Path,
    ) -> tuple[dict[str, Any], ...]:
        evidence = json.loads((root / "output/t1-preparation-evidence.json").read_text())
        if evidence.get("operation") != "t1_preparation":
            raise ValueError("invalid T1 preparation evidence")
        rows = evidence["outputs"]
        if isinstance(rows, dict):
            rows = [rows]
        expected = tuple(subject.subject_id for subject in compilation.subjects)
        if tuple(row["subject_id"] for row in rows) != expected:
            raise ValueError("T1 preparation subjects differ")
        for row in rows:
            metadata = row["metadata"]
            if metadata["volume_count"] != 1 or len(metadata["dimensions"]) != 3:
                raise ValueError("prepared T1 must be 3D")
            subject = row["subject_id"]
            paths = {row["relative_path"], row["inspection_image"]}
            if any(not path.startswith(f"output/t1/{subject}/") for path in paths):
                raise ValueError("T1 preparation path differs from subject")
            if row["relative_path"] not in {artifact["relative_path"] for artifact in artifacts}:
                raise ValueError("T1 preparation products missing")
            verify_t1_geometry(root / row["relative_path"], metadata)
            for artifact in artifacts:
                if artifact["relative_path"] in paths:
                    validate_t1_artifact(
                        root / artifact["relative_path"], artifact["artifact_type"]
                    )
                    artifact["provenance"].update(
                        subject_id=subject,
                        manifest_hash=compilation.spec.input_manifest_hash,
                        metadata=metadata,
                        operation="t1_preparation",
                    )
        return artifacts

    def _preprocessing_lineage(
        self,
        artifacts: tuple[dict[str, Any], ...],
        compilation: PreprocessingCompilation,
        root: Path,
    ) -> tuple[dict[str, Any], ...]:
        assert isinstance(compilation.spec.payload, PreprocessingJobPayload)
        if compilation.spec.payload.t1_preparation is not None:
            artifacts = self._t1_preparation_lineage(artifacts, compilation, root)
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
                if artifact["artifact_type"].startswith(
                    ("preprocessing.tissue_", "preprocessing.dartel_")
                ):
                    artifact["provenance"].update(
                        manifest_hash=compilation.spec.input_manifest_hash,
                        subject_ids=list(compilation.spec.payload.subject_ids),
                        subject_id=(
                            None
                            if artifact["artifact_type"] == "preprocessing.dartel_template"
                            else PurePosixPath(path).parent.name
                        ),
                        source_artifact_ids=[
                            binding.artifact_id
                            for binding in compilation.spec.artifact_bindings
                            if "/T1Img/" in binding.relative_path
                        ],
                        cohort_hash=stable_hash(compilation.spec.payload.subject_ids),
                    )
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
            if metric_parameters is not None and metric != "brain_mask":
                if metric_parameters.mask_source is MetricMaskSource.REGISTERED_ARTIFACT:
                    mask_id = metric_parameters.mask_artifact_id
                else:
                    generated_mask = (
                        PurePosixPath("attempts")
                        / root.name
                        / f"output/{output.subject_id}/brain_mask.nii"
                    ).as_posix()
                    mask_id = f"generated-output:{generated_mask}"
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
                "brain_mask": ArtifactKind.BRAIN_MASK,
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
                "brain_mask": "calculate_alff_falff",
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


def _is_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    if callable(checker):
        return bool(checker())
    try:
        return bool(getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400)
    except OSError:
        return True


def _predict_dpabi_stage(stage: str, cfg: dict[str, Any]) -> str:
    """Predict the next DPABI checkpoint from the verified append order."""

    suffixes = (
        ("IsSliceTiming", "A"),
        ("IsRealign", "R"),
        ("IsCovremove", "C"),
        ("IsFilter", "F"),
        ("IsNormalize", "W"),
        ("IsSmooth", "S"),
        ("IsDetrend", "D"),
        ("IsScrubbing", "B"),
    )
    result = stage
    for field, suffix in suffixes:
        if cfg.get(field, 0):
            result += suffix
    return result
