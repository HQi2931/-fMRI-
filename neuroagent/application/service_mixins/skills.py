"""Reviewed Skill compilation and resolution use cases."""

from __future__ import annotations

from pydantic import ValidationError

from neuroagent.application.contracts import (
    DatasetKind,
    ManifestRevisionView,
    SkillPlanIntent,
    SkillPlanResolveRequest,
    SkillPlanResolveView,
    ValidationIssue,
)
from neuroagent.application.errors import ConflictError, InputValidationError
from neuroagent.application.hashing import content_hash
from neuroagent.application.service_mixins._base import BaseServiceMixin
from neuroagent.domain.fmri.artifacts import ArtifactKind, ArtifactLineage
from neuroagent.domain.fmri.preprocessing import NormalizationMode
from neuroagent.skills.compiler import SkillCompileError
from neuroagent.skills.models import SkillPlan, SkillRequest, SkillSpec


class SkillPlanMixin(BaseServiceMixin):
    def list_skills(self) -> list[SkillSpec]:
        return list(self.skill_registry.list())

    @staticmethod
    def _validate_manifest_for_skill_intent(
        intent: SkillPlanIntent,
        manifest: ManifestRevisionView,
    ) -> None:
        if manifest.profile.kind is DatasetKind.DICOM:
            raise InputValidationError(
                "manifest_dicom_inventory_only",
                "DICOM 清单当前仅用于只读 inventory; "
                "完成显式序列角色映射和受控转换后才能创建预处理或指标计划。",
            )
        if manifest.profile.kind is DatasetKind.DPABI_READY:
            supported_stages = {"funraw", "funimg"}
            functional_stage_roots = sorted(
                {
                    relative_path.split("/", maxsplit=1)[0].lower()
                    for entry in manifest.subjects
                    for relative_path in entry.functional_files
                }
            )
            if (
                len(functional_stage_roots) != 1
                or functional_stage_roots[0] not in supported_stages
            ):
                raise InputValidationError(
                    "manifest_dpabi_input_stage_invalid",
                    "DPABI-ready manifest 必须只绑定一个受支持的功能输入 stage, "
                    "不能混合中间 checkpoint。",
                    functional_stage_roots=functional_stage_roots,
                    supported_input_stage_roots=sorted(supported_stages),
                )
            expected_anatomical_stage = {
                "funraw": "t1raw",
                "funimg": "t1img",
            }[functional_stage_roots[0]]
            invalid_anatomical_paths = sorted(
                relative_path
                for entry in manifest.subjects
                for relative_path in entry.anatomical_files
                if relative_path.split("/", maxsplit=1)[0].lower() != expected_anatomical_stage
            )
            if invalid_anatomical_paths:
                raise InputValidationError(
                    "manifest_dpabi_anatomical_stage_mixed",
                    "DPABI-ready manifest 的结构像不能来自未配对的 T1 checkpoint。",
                    expected_anatomical_stage=expected_anatomical_stage,
                    invalid_relative_paths=invalid_anatomical_paths,
                )
        missing_functional = [
            {
                "subject_id": entry.subject_id,
                "session_id": entry.session_id,
            }
            for entry in manifest.subjects
            if not entry.functional_files
        ]
        if not manifest.subjects or missing_functional:
            raise InputValidationError(
                "manifest_functional_input_missing",
                "冻结 manifest 中每个受试者/会话都必须包含明确识别的功能 BOLD 输入; "
                "DICOM inventory 不能替代角色映射。",
                missing=missing_functional,
            )

        if manifest.profile.kind in {DatasetKind.BIDS, DatasetKind.NIFTI, DatasetKind.MIXED}:
            ambiguous_functional = [
                {
                    "subject_id": entry.subject_id,
                    "session_id": entry.session_id,
                    "candidate_relative_paths": list(entry.functional_files),
                }
                for entry in manifest.subjects
                if len(entry.functional_files) > 1
            ]
            if ambiguous_functional:
                raise InputValidationError(
                    "manifest_functional_input_ambiguous",
                    "受试者/会话存在多个功能候选, 当前计划未提供显式 run/文件选择, "
                    "禁止按文件排序隐式选取。",
                    ambiguous=ambiguous_functional,
                )

        preprocessing = intent.preprocessing
        if preprocessing is None or preprocessing.normalization.mode not in {
            NormalizationMode.T1_SEGMENT,
            NormalizationMode.DARTEL,
        }:
            return
        missing_anatomical = [
            {
                "subject_id": entry.subject_id,
                "session_id": entry.session_id,
                "candidate_relative_paths": list(entry.anatomical_files),
            }
            for entry in manifest.subjects
            if not entry.anatomical_files
        ]
        if missing_anatomical:
            raise InputValidationError(
                "manifest_anatomical_input_missing",
                "T1 分割或 DARTEL 要求每个受试者/会话都有明确识别的结构像。",
                missing=missing_anatomical,
            )
        ambiguous_anatomical = [
            {
                "subject_id": entry.subject_id,
                "session_id": entry.session_id,
                "candidate_relative_paths": list(entry.anatomical_files),
            }
            for entry in manifest.subjects
            if len(entry.anatomical_files) > 1
        ]
        if ambiguous_anatomical:
            raise InputValidationError(
                "manifest_anatomical_input_ambiguous",
                "T1 分割或 DARTEL 要求每个受试者/会话精确匹配一份结构像。",
                ambiguous=ambiguous_anatomical,
            )
        expected_structural_ref = f"manifest:{manifest.manifest_id}:anatomical-source"
        if preprocessing.normalization.structural_artifact_id != expected_structural_ref:
            raise InputValidationError(
                "structural_artifact_binding_invalid",
                "结构像引用必须由服务端冻结 manifest 绑定, 不能使用任意客户端 Artifact ID。",
                expected_structural_artifact_id=expected_structural_ref,
            )

    def _materialize_skill_request(
        self,
        intent: SkillPlanIntent,
        manifest: ManifestRevisionView,
    ) -> SkillRequest:
        if intent.request_preprocessing:
            source_ref = f"manifest:{manifest.manifest_id}:functional-source"
            input_artifact = ArtifactLineage(
                artifact_id=source_ref,
                kind=ArtifactKind.FUNCTIONAL_TIMESERIES,
                metadata_verified=False,
                subject_manifest_hash=manifest.content_hash,
                space="source-uninspected",
                grid_signature=f"manifest-{manifest.content_hash[:16]}",
                voxel_size_mm=(1.0, 1.0, 1.0),
                mask_artifact_id=None,
                mask_grid_signature=None,
                temporally_filtered=False,
                frequency_band=None,
                spatially_smoothed=False,
                smoothing_fwhm_mm=None,
                scrubbed=False,
                producer_step_hash=content_hash(
                    {
                        "kind": "frozen_manifest_functional_source",
                        "manifest_id": manifest.manifest_id,
                        "manifest_hash": manifest.content_hash,
                    }
                ),
            )
            base_cfg_artifact_id = f"builtin:dpabi-v82-base-cfg:{self.settings.adapter_version}"
        else:
            assert intent.input_artifact_id is not None
            artifact = self.repository.get_artifact(intent.input_artifact_id)
            if artifact.project_id != intent.project_id:
                raise ConflictError(
                    "cross_project_artifact",
                    "Skill 输入 Artifact 不属于当前项目。",
                )
            lineage_data = artifact.provenance.get("lineage")
            if not isinstance(lineage_data, dict):
                raise InputValidationError(
                    "artifact_lineage_missing",
                    "指标规划只能使用带有服务端登记类型化 lineage 的 Artifact。",
                    artifact_id=artifact.artifact_id,
                )
            try:
                input_artifact = ArtifactLineage.model_validate(lineage_data)
            except ValidationError as exc:
                raise InputValidationError(
                    "artifact_lineage_invalid",
                    "输入 Artifact 的登记 lineage 无效。",
                    artifact_id=artifact.artifact_id,
                ) from exc
            if input_artifact.artifact_id != artifact.artifact_id:
                raise InputValidationError(
                    "artifact_lineage_identity_mismatch",
                    "输入 Artifact ID 与登记 lineage 不一致。",
                )
            if input_artifact.subject_manifest_hash != manifest.content_hash:
                raise ConflictError(
                    "artifact_manifest_mismatch",
                    "输入 Artifact lineage 不属于当前冻结 manifest。",
                )
            source_run = self.repository.get_run(artifact.run_id)
            source_plan = self.repository.get_plan(source_run.plan_revision_id)
            if source_plan.manifest_hash != manifest.content_hash:
                raise ConflictError(
                    "artifact_plan_manifest_mismatch",
                    "输入 Artifact 的来源计划未绑定当前冻结 manifest。",
                )
            base_cfg_artifact_id = None

        return SkillRequest(
            project_id=intent.project_id,
            dataset_ref=intent.dataset_ref,
            input_manifest_hash=manifest.content_hash,
            requested_metrics=intent.requested_metrics,
            primary_outputs=intent.primary_outputs,
            input_artifact=input_artifact,
            alff_falff=intent.alff_falff,
            reho=intent.reho,
            study_protocol_ref=intent.study_protocol_ref,
            request_preprocessing=intent.request_preprocessing,
            preprocessing=intent.preprocessing,
            base_cfg_artifact_id=base_cfg_artifact_id,
        )

    def resolve_skill_plan(
        self, request: SkillPlanResolveRequest, idempotency_key: str
    ) -> SkillPlanResolveView:
        def prepare() -> tuple[str, SkillPlan, list[ValidationIssue]]:
            self._require_project_version(
                request.request.project_id, request.expected_project_version
            )
            dataset = self.repository.get_dataset(request.request.dataset_ref)
            if dataset.project_id != request.request.project_id:
                raise ConflictError(
                    "cross_project_dataset",
                    "Skill 请求引用的数据集不属于当前项目。",
                )
            if dataset.current_manifest_id is None:
                raise ConflictError(
                    "dataset_manifest_missing",
                    "Skill 请求引用的数据集尚未生成冻结 manifest。",
                )
            manifest = self.repository.get_manifest(dataset.current_manifest_id)
            if manifest.content_hash != request.request.input_manifest_hash:
                raise ConflictError(
                    "manifest_hash_mismatch",
                    "Skill 请求的输入清单哈希不是数据集当前 manifest。",
                    expected=manifest.content_hash,
                    received=request.request.input_manifest_hash,
                )
            self._validate_manifest_for_skill_intent(request.request, manifest)
            skill_request = self._materialize_skill_request(request.request, manifest)
            environment = self.environment_provider.current().snapshot
            resolution = self.skill_resolver.resolve(skill_request, environment)
            report = self.skill_validator.validate_resolution(resolution)
            if report.has_blockers:
                raise InputValidationError(
                    "skill_plan_blocked",
                    "Skill 方案存在阻断问题, 不能编译或进入审批。",
                    issues=[issue.model_dump(mode="json") for issue in report.issues],
                )
            try:
                skill_plan = self.skill_compiler.compile(resolution)
            except SkillCompileError as exc:
                raise InputValidationError(
                    "skill_compile_failed", "Skill 方案编译失败。", reason=str(exc)
                ) from exc
            warnings = [
                ValidationIssue(
                    code=issue.code,
                    message=issue.message,
                    severity=issue.severity.value,
                    path=issue.path,
                )
                for issue in report.issues
            ]
            return manifest.manifest_id, skill_plan, warnings

        def finalize(
            prepared: tuple[str, SkillPlan, list[ValidationIssue]],
        ) -> SkillPlanResolveView:
            manifest_id, skill_plan, warnings = prepared
            dataset = self.repository.get_dataset(request.request.dataset_ref)
            if dataset.current_manifest_id != manifest_id:
                raise ConflictError(
                    "dataset_manifest_changed_during_prepare",
                    "Skill 方案准备期间数据集 manifest 已变化, 请重新生成方案。",
                )
            manifest = self.repository.get_manifest(manifest_id)
            if manifest.content_hash != skill_plan.input_manifest_hash:
                raise ConflictError(
                    "manifest_hash_changed_during_prepare",
                    "Skill 方案准备期间冻结 manifest 已失配。",
                )
            stored = self.repository.create_plan(
                project_id=skill_plan.project_id,
                expected_project_version=request.expected_project_version,
                plan={
                    "kind": "skill_plan",
                    "dataset_manifest_id": manifest_id,
                    "skill_plan": skill_plan.model_dump(mode="json"),
                },
                plan_hash=skill_plan.plan_hash,
                manifest_hash=skill_plan.input_manifest_hash,
                environment_hash=skill_plan.environment.environment_hash,
                supersedes_id=request.supersedes_plan_revision_id,
            )
            stored = self.repository.validate_plan(
                stored.plan_revision_id,
                expected_version=stored.version,
                issues=warnings,
            )
            self.repository.append_event(
                project_id=stored.project_id,
                run_id=None,
                event_type="SkillPlanCompiled",
                severity="warning" if warnings else "info",
                payload={
                    "plan_revision_id": stored.plan_revision_id,
                    "plan_hash": stored.plan_hash,
                    "skill_ids": [lock.skill_id for lock in skill_plan.skill_locks],
                    "warning_count": len(warnings),
                },
            )
            return SkillPlanResolveView(skill_plan=skill_plan, plan_revision=stored)

        return self._idempotent_prepared(
            scope=f"projects:{request.request.project_id}:skill-plans:resolve",
            key=idempotency_key,
            request=request,
            response_type=SkillPlanResolveView,
            prepare=prepare,
            finalize=finalize,
        )
