"""Statistical design creation and plan-current validation use cases."""

# ruff: noqa: RUF001

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from neuroagent.analysis.dpabi_atlas import list_dpabi_atlases
from neuroagent.analysis.external_metric_import import (
    inspect_external_mask,
    inspect_external_metric_directory,
)
from neuroagent.application.automation.service import WorkTaskApplicationService
from neuroagent.application.contracts.external_imports import ExternalAnalysisImportView
from neuroagent.application.contracts.plans import PlanRevisionView
from neuroagent.application.contracts.runs import ArtifactView
from neuroagent.application.contracts.statistics import (
    CorrectionCapabilityView,
    StatisticalDesignCreate,
    StatisticalDesignValidationRequest,
    StatisticalDesignView,
)
from neuroagent.application.errors import ApplicationError, ConflictError, InputValidationError
from neuroagent.application.hashing import content_hash
from neuroagent.application.repository_ports import (
    ArtifactCatalogRepositoryPort,
    ConversationRepositoryPort,
    ExternalAnalysisRepositoryPort,
    PlanRepositoryPort,
    QcRepositoryPort,
    RunRepositoryPort,
)
from neuroagent.application.service_mixins._base import BaseServiceMixin
from neuroagent.application.statistical_capabilities import STATISTICAL_CAPABILITIES
from neuroagent.application.statistics_draft import (
    ArtifactRef,
    DesignIssue,
    DesignSubject,
    QcBinding,
    StatisticalDesignProposal,
    StatisticsChoices,
    StatisticsDraft,
    assemble_statistical_design,
    build_correction,
    collect_design_sources,
    proposal_hash,
    render_statistics_draft_review,
    statistics_draft_hash,
)
from neuroagent.application.work_handoffs import (
    WorkAnalysisHandoff,
    WorkPreprocessingHandoff,
    prepare_analysis_handoff,
    preprocessing_handoff_hash,
    preprocessing_output_requirements,
    require_same_handoff,
)
from neuroagent.application.work_tasks import ensure_statistical_work_task_for_plan
from neuroagent.domain.fmri.artifacts import ArtifactKind, ArtifactLineage
from neuroagent.domain.fmri.external_import import external_metric_artifact_id
from neuroagent.domain.fmri.qc import assert_statistics_ready
from neuroagent.domain.fmri.results import StatisticalAtlasSelection
from neuroagent.domain.fmri.statistics import (
    FdrCorrection,
    GrfCorrection,
    MissingValuePolicy,
    StatisticalDesignRevision,
    StatisticalQcSource,
    StatisticalTest,
    Tail,
    design_matrix,
    validate_correction_for_design,
)
from neuroagent.domain.lifecycle import WorkflowState
from neuroagent.skills.models import SkillPlan
from neuroagent.skills.registry import SkillRegistryError
from neuroagent.tools.registry import ToolRegistryError

#: Mask artifact type produced by the controlled preprocessing path.
_BRAIN_MASK_TYPE = "preprocessing.brain_mask"


def _design_issue(code: str, message: str) -> DesignIssue:
    return DesignIssue(code=code, message=message)


def _artifact_ref(artifact: ArtifactView) -> ArtifactRef:
    """Read subject identity from the registered lineage, never from the path."""

    lineage = artifact.provenance.get("lineage")
    subject_id = lineage.get("subject_id") if isinstance(lineage, dict) else None
    return ArtifactRef(
        artifact_id=artifact.artifact_id,
        artifact_type=artifact.artifact_type,
        subject_id=subject_id if isinstance(subject_id, str) and subject_id else None,
    )


def _choices_from_draft(draft: StatisticsDraft) -> StatisticsChoices:
    return StatisticsChoices(
        test=draft.test or StatisticalTest.ONE_SAMPLE_T,
        grouping_column=draft.grouping_column,
        group_order=tuple(draft.group_order),
        target_predictor=draft.target_predictor,
        condition_column=draft.condition_column,
        condition_order=tuple(draft.condition_order),
        covariates=tuple(draft.covariates),
        tail=draft.tail or Tail.TWO_SIDED,
        one_sample_baseline=draft.one_sample_baseline,
        missing_value_policy=draft.missing_value_policy or MissingValuePolicy.ERROR,
    )


def _validate_statistical_locks(
    service: Any, plan: PlanRevisionView, reasons: list[dict[str, str]]
) -> None:
    """Append drift reasons for frozen statistical Skill and Tool locks."""

    expected_hash = content_hash(
        {
            "plan": plan.plan,
            "manifest_hash": plan.manifest_hash,
            "environment_hash": plan.environment_hash,
        }
    )
    if expected_hash != plan.plan_hash:
        reasons.append(
            {
                "code": "statistical_plan_hash_changed",
                "message": "stored statistical design no longer matches its approved hash",
            }
        )
    for lock_data in plan.plan.get("skill_locks", []):
        try:
            current = service.skill_registry.resolve(
                str(lock_data["skill_id"]), str(lock_data["version"])
            )
            if current.content_hash != str(lock_data["content_hash"]):
                raise SkillRegistryError("content hash changed")
        except (KeyError, TypeError, SkillRegistryError):
            reasons.append(
                {
                    "code": "statistics_skill_lock_changed",
                    "message": str(lock_data),
                }
            )
    for lock_data in plan.plan.get("tool_locks", []):
        try:
            capability = str(lock_data["capability"])
            current_tool = service.tool_registry.resolve_capability(capability)
            if current_tool.model_dump(mode="json") != lock_data:
                raise ToolRegistryError("content hash changed")
        except (KeyError, TypeError, ToolRegistryError):
            reasons.append(
                {
                    "code": "statistics_tool_lock_changed",
                    "message": str(lock_data),
                }
            )
    if not plan.plan.get("skill_locks") or not plan.plan.get("tool_locks"):
        reasons.append(
            {
                "code": "statistics_execution_locks_missing",
                "message": "statistical plan lacks frozen Skill/Tool locks",
            }
        )


class StatisticsMixin(BaseServiceMixin):
    automation: WorkTaskApplicationService
    artifact_repository: ArtifactCatalogRepositoryPort
    conversation_repository: ConversationRepositoryPort
    external_import_repository: ExternalAnalysisRepositoryPort
    plan_repository: PlanRepositoryPort
    qc_repository: QcRepositoryPort
    run_repository: RunRepositoryPort

    def _freeze_statistical_atlas(self, atlas_id: str) -> StatisticalAtlasSelection:
        configuration = self.environment_provider.configuration()
        availability = next(
            (
                item
                for item in list_dpabi_atlases(configuration.dpabi_dir)
                if item.atlas_id == atlas_id
            ),
            None,
        )
        if availability is None or not availability.available or availability.content_hash is None:
            raise InputValidationError(
                "atlas_unavailable", "所选图谱不可用，请选择已验证图谱或暂不指定图谱。"
            )
        return StatisticalAtlasSelection.model_validate(
            {
                "atlas_id": availability.atlas_id,
                "atlas_name": availability.name,
                "atlas_content_hash": availability.content_hash,
                "coordinate_space": availability.space,
            }
        )

    def _validate_statistical_atlas(self, plan: PlanRevisionView) -> None:
        raw = plan.plan.get("localization_plan")
        if raw is None:
            return
        frozen = StatisticalAtlasSelection.model_validate(raw)
        current = self._freeze_statistical_atlas(frozen.atlas_id)
        if current != frozen:
            raise ConflictError(
                "statistics_atlas_changed", "所选图谱已变化，请重新建立并审查分析方案。"
            )

    @staticmethod
    def _validate_correction(
        design: StatisticalDesignRevision,
        correction: FdrCorrection | GrfCorrection | None,
    ) -> None:
        try:
            validate_correction_for_design(design, correction)
        except ValueError as exc:
            raise InputValidationError(
                "invalid_correction_for_design",
                "多重比较校正参数与冻结的统计设计不一致。",
                reason=str(exc),
            ) from exc

    def _require_skill_parameter_schema(self, skill_directory: str, payload: object) -> None:
        report = self.skill_validator.validate_parameter_payload(skill_directory, payload)
        if not report.has_blockers:
            return
        raise InputValidationError(
            "skill_parameter_schema_invalid",
            "请求参数不符合内置 Skill 的运行时 Schema。",
            skill_directory=skill_directory,
            issues=[issue.model_dump(mode="json") for issue in report.issues],
        )

    @staticmethod
    def _statistics_view(plan: PlanRevisionView) -> StatisticalDesignView:
        if plan.plan.get("kind") != "statistical_design":
            raise InputValidationError(
                "not_statistical_design",
                "指定的计划版本不是统计设计。",
                plan_revision_id=plan.plan_revision_id,
            )
        design = StatisticalDesignRevision.model_validate(plan.plan.get("design"))
        correction_data = plan.plan.get("correction")
        correction: FdrCorrection | GrfCorrection | None = None
        if isinstance(correction_data, dict):
            if correction_data.get("method") == "fdr":
                correction = FdrCorrection.model_validate(correction_data)
            elif correction_data.get("method") == "grf":
                correction = GrfCorrection.model_validate(correction_data)
            else:
                raise InputValidationError(
                    "unsupported_correction", "统计设计包含不受支持的校正方法。"
                )
        matrix = design_matrix(design)
        stored_matrix = plan.plan.get("design_matrix")
        if stored_matrix != [list(row) for row in matrix]:
            raise InputValidationError(
                "design_matrix_integrity_failed",
                "冻结的设计矩阵与统计设计不一致。",
            )
        StatisticsMixin._validate_correction(design, correction)
        return StatisticalDesignView(
            design=design,
            localization_plan=plan.plan.get("localization_plan"),
            correction=correction,
            design_matrix=matrix,
            plan_revision=plan,
        )

    def _statistics_execution_locks(
        self, correction: FdrCorrection | GrfCorrection | None
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        skill_ids = ["rsfmri.statistics.ttest"]
        if correction is not None:
            skill_ids.append(f"rsfmri.statistics.{correction.method}")
        specs = [self.skill_registry.resolve(skill_id) for skill_id in skill_ids]
        skill_locks = [
            {
                "skill_id": spec.skill_id,
                "version": spec.version,
                "content_hash": spec.content_hash,
            }
            for spec in sorted(specs, key=lambda item: item.skill_id)
        ]
        capabilities = sorted(
            {capability for spec in specs for capability in spec.required_capabilities}
        )
        tool_locks = [
            self.tool_registry.resolve_capability(capability).model_dump(mode="json")
            for capability in capabilities
        ]
        return skill_locks, tool_locks

    @staticmethod
    def _registered_lineage(artifact: ArtifactView) -> ArtifactLineage:
        lineage_data = artifact.provenance.get("lineage")
        if not isinstance(lineage_data, dict):
            raise InputValidationError(
                "artifact_lineage_missing",
                "统计输入必须使用带有服务端登记类型化 lineage 的 Artifact。",
                artifact_id=artifact.artifact_id,
            )
        try:
            lineage = ArtifactLineage.model_validate(lineage_data)
        except ValidationError as exc:
            raise InputValidationError(
                "artifact_lineage_invalid",
                "统计输入 Artifact 的登记 lineage 无效。",
                artifact_id=artifact.artifact_id,
            ) from exc
        if lineage.artifact_id != artifact.artifact_id:
            raise InputValidationError(
                "artifact_lineage_identity_mismatch",
                "统计输入 Artifact ID 与登记 lineage 不一致。",
                artifact_id=artifact.artifact_id,
            )
        return lineage

    def _validate_statistical_artifact_lineage(
        self,
        design: StatisticalDesignRevision,
        *,
        manifest_hash: str,
        source_run_id: str,
    ) -> None:
        metric_kinds = {
            ArtifactKind.ALFF_MAP,
            ArtifactKind.FALFF_MAP,
            ArtifactKind.REHO_MAP,
        }
        mask_artifact = self.artifact_repository.get_artifact(design.mask_artifact_id)
        if mask_artifact.run_id != source_run_id:
            raise ConflictError(
                "statistics_mask_run_mismatch",
                "统计 mask 必须属于已批准 QC 的来源运行。",
            )
        mask = self._registered_lineage(mask_artifact)
        if (
            mask.kind is not ArtifactKind.BRAIN_MASK
            or not mask.metadata_verified
            or mask.subject_manifest_hash != manifest_hash
            or mask.subject_id is not None
        ):
            raise InputValidationError(
                "statistics_mask_lineage_invalid",
                "统计 mask 必须是同 manifest、元数据已验证的组级 BRAIN_MASK Artifact。",
            )

        reference_signature: tuple[object, ...] | None = None
        for image in design.images:
            artifact = self.artifact_repository.get_artifact(image.artifact_id)
            if artifact.run_id != source_run_id:
                raise ConflictError(
                    "statistics_artifact_run_mismatch",
                    "统计影像必须属于已批准 QC 的来源运行。",
                    artifact_id=artifact.artifact_id,
                )
            lineage = self._registered_lineage(artifact)
            if lineage.kind not in metric_kinds or not lineage.metadata_verified:
                raise InputValidationError(
                    "statistics_metric_lineage_invalid",
                    "统计输入必须是元数据已验证的受试者级 ALFF/fALFF/ReHo Artifact。",
                    artifact_id=artifact.artifact_id,
                )
            if lineage.subject_manifest_hash != manifest_hash:
                raise ConflictError(
                    "statistics_artifact_manifest_mismatch",
                    "统计 Artifact lineage 与冻结 manifest 不一致。",
                    artifact_id=artifact.artifact_id,
                )
            if lineage.subject_id != image.subject_id:
                raise InputValidationError(
                    "statistics_artifact_subject_mismatch",
                    "AnalysisImage.subject_id 必须等于 Artifact lineage.subject_id。",
                    artifact_id=artifact.artifact_id,
                    expected=lineage.subject_id,
                    received=image.subject_id,
                )
            if lineage.condition != image.condition:
                raise InputValidationError(
                    "statistics_artifact_condition_mismatch",
                    "AnalysisImage.condition 必须等于 Artifact lineage.condition。",
                    artifact_id=artifact.artifact_id,
                    expected=lineage.condition,
                    received=image.condition,
                )
            if lineage.mask_artifact_id != design.mask_artifact_id:
                raise InputValidationError(
                    "statistics_metric_mask_mismatch",
                    "所有统计指标图必须引用统计设计中冻结的同一 mask Artifact。",
                    artifact_id=artifact.artifact_id,
                )
            if lineage.grid_signature != mask.grid_signature:
                raise InputValidationError(
                    "statistics_metric_grid_mismatch",
                    "统计指标图与 mask 的网格签名不一致。",
                    artifact_id=artifact.artifact_id,
                )
            band = (
                None
                if lineage.frequency_band is None
                else (lineage.frequency_band.low_hz, lineage.frequency_band.high_hz)
            )
            signature = (
                lineage.kind,
                lineage.metric_scaling,
                # Paired conditions may deliberately come from two sessions;
                # the explicit condition order and per-image lineage carry
                # that pairing while all processing attributes must match.
                None if design.test is StatisticalTest.PAIRED_T else lineage.session_id,
                lineage.space,
                lineage.grid_signature,
                lineage.voxel_size_mm,
                lineage.mask_artifact_id,
                lineage.mask_grid_signature,
                lineage.temporally_filtered,
                band,
                lineage.spatially_smoothed,
                lineage.smoothing_fwhm_mm,
                lineage.scrubbed,
            )
            if reference_signature is None:
                reference_signature = signature
            elif signature != reference_signature:
                raise InputValidationError(
                    "statistics_metric_series_mismatch",
                    "同一统计设计中的指标类型、缩放、空间、网格与处理谱系必须一致。",
                    artifact_id=artifact.artifact_id,
                )

    def _validate_external_statistical_source(
        self,
        request: StatisticalDesignCreate,
        *,
        verify_source_files: bool,
    ) -> ExternalAnalysisImportView:
        design = request.design
        assert design.external_import_id is not None
        assert design.external_import_hash is not None
        imported = self.external_import_repository.get_external_analysis_import(
            design.external_import_id
        )
        inclusion = imported.inclusion
        selection = imported.selection
        if imported.project_id != request.project_id:
            raise ConflictError("cross_project_external_import", "外部分析输入不属于当前项目。")
        if (
            imported.status != "included"
            or inclusion is None
            or selection is None
            or imported.inclusion_hash != design.external_import_hash
        ):
            raise ConflictError(
                "external_import_not_selectable",
                "统计设计必须引用已纳入且已登记同网格 mask 的外部指标目录。",
            )
        if request.input_manifest_hash != selection.input_manifest_hash:
            raise ConflictError(
                "external_import_manifest_mismatch",
                "统计设计的外部输入清单哈希与纳入记录不一致。",
            )
        paired = design.test is StatisticalTest.PAIRED_T
        if paired != (imported.inspection.layout == "paired_conditions"):
            raise InputValidationError(
                "external_import_layout_mismatch", "检验类型必须与已确认的分组或配对条件布局一致。"
            )
        if paired and set(design.condition_order) != set(selection.group_order):
            raise InputValidationError(
                "external_import_condition_mismatch", "配对条件必须与纳入记录一致。"
            )
        selected = {(item.subject_id, item.condition): item for item in selection.images}
        if {(item.subject_id, item.condition) for item in design.images} != set(selected):
            raise InputValidationError(
                "external_import_pair_alignment_failed", "影像的受试者与条件必须逐项对应纳入记录。"
            )
        if len(design.images) != len(selected) or set(design.subject_order) != {
            item.subject_id for item in selection.images
        }:
            raise InputValidationError(
                "external_import_subject_alignment_failed",
                "统计设计必须明确对齐纳入记录中的全部受试者。",
            )
        grouped = design.test in {
            StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
            StatisticalTest.ONE_WAY_ANOVA_ANCOVA,
        }
        if grouped and set(design.group_order) != set(selection.group_order):
            raise InputValidationError(
                "external_import_group_alignment_failed",
                "组间统计的组别必须与外部纳入记录一致；方向由设计中的组别顺序确定。",
            )
        for image in design.images:
            source = selected[(image.subject_id, image.condition)]
            if (
                image.artifact_id
                != external_metric_artifact_id(
                    imported.import_id, image.subject_id, image.condition
                )
                or image.artifact_id != source.artifact_id
            ):
                raise InputValidationError(
                    "external_import_image_reference_invalid",
                    "统计影像引用与外部纳入记录不一致。",
                    subject_id=image.subject_id,
                )
            if (
                image.group != (source.group if grouped else None)
                or image.condition != source.condition
            ):
                raise InputValidationError(
                    "external_import_label_alignment_failed",
                    "统计影像的组别或条件与外部纳入记录不一致。",
                    subject_id=image.subject_id,
                )
        if design.mask_artifact_id != selection.mask_artifact_id:
            raise InputValidationError(
                "external_import_mask_reference_invalid",
                "统计 mask 引用与外部纳入记录不一致。",
            )
        if design.covariates and not design.external_demographics_revision_id:
            raise InputValidationError(
                "external_demographics_required", "外部协变量必须绑定登记的人口学修订。"
            )
        if design.external_demographics_revision_id:
            revision, content = self.external_import_repository.get_external_demographics_content(
                design.external_demographics_revision_id
            )
            latest = self.external_import_repository.get_latest_external_demographics(
                imported.import_id
            )
            if (
                revision.external_import_id != imported.import_id
                or revision.inclusion_hash != imported.inclusion_hash
                or revision.content_hash != design.external_demographics_hash
                or latest is None
                or latest.demographics_id != revision.demographics_id
            ):
                raise ConflictError(
                    "external_demographics_changed",
                    "人口学修订已变化或不属于当前外部来源，请重新准备统计方案。",
                )
            rows = {str(row["subject_id"]): row for row in content["rows"]}
            for column in design.covariates:
                for entry in column.values:
                    try:
                        registered = float(rows[entry.subject_id][column.name])
                    except (KeyError, TypeError, ValueError) as exc:
                        raise InputValidationError(
                            "external_covariate_unavailable", "所选人口学数值缺失或无效。"
                        ) from exc
                    if registered != entry.value:
                        raise InputValidationError(
                            "external_covariate_value_mismatch",
                            "协变量取值必须来自冻结的人口学修订。",
                        )
        if verify_source_files:
            project = self.project_repository.get_project(imported.project_id)
            source_root = self.path_policy.validate_read_path(
                imported.source_path,
                project_roots=project.source_roots,
                expect_directory=True,
            )
            try:
                current = inspect_external_metric_directory(
                    source_root, layout=imported.inspection.layout
                )
            except (OSError, ValueError) as exc:
                raise ConflictError(
                    "external_import_source_unavailable", "外部指标目录无法重新验证。"
                ) from exc
            if (
                current.blocking_issues
                or current.source_tree_hash != imported.inspection.source_tree_hash
            ):
                raise ConflictError(
                    "external_import_source_changed",
                    "外部指标目录在纳入后发生变化；请重新检查并纳入。",
                )
            assert inclusion.mask_relative_path is not None
            assert inclusion.mask_sha256 is not None
            try:
                mask_hash, mask_size, mask_grid = inspect_external_mask(
                    source_root, inclusion.mask_relative_path
                )
            except (OSError, ValueError) as exc:
                raise ConflictError(
                    "external_import_mask_unavailable", "外部 mask 无法重新验证。"
                ) from exc
            if (
                mask_hash != inclusion.mask_sha256
                or mask_size != inclusion.mask_size_bytes
                or mask_grid != inclusion.mask_grid_signature
                or mask_grid != current.grid_signature
            ):
                raise ConflictError(
                    "external_import_mask_changed",
                    "外部 mask 在纳入后发生变化；请重新检查并纳入。",
                )
        return imported

    def prepare_work_analysis_handoff(
        self, run_id: str, project_id: str, *, verify_files: bool = True
    ) -> WorkAnalysisHandoff:
        proof = prepare_analysis_handoff(
            self.run_repository,
            self.plan_repository,
            self.project_repository,
            self.artifact_repository,
            self._statistics_source_binding,
            run_id=run_id,
            project_id=project_id,
            verify_files=verify_files,
        )
        if proof.status == "ready" and verify_files:
            try:
                dataset = self.project_repository.get_dataset(
                    str(proof.data_references["dataset_id"])
                )
                project = self.project_repository.get_project(project_id)
                manifest = self.project_repository.get_manifest(
                    str(proof.data_references["dataset_manifest_id"])
                )
                source = self.path_policy.validate_read_path(
                    dataset.source_path, project_roots=project.source_roots, expect_directory=True
                )
                inspected = self._inspect_source(
                    source, report_only=False, selected_input_stage=manifest.selected_input_stage
                )
                if content_hash(inspected) != manifest.content_hash:
                    raise ConflictError("source_manifest_content_changed", "Source files changed.")
            except ApplicationError as exc:
                proof = proof.model_copy(
                    update={
                        "status": "blocked",
                        "blocking_items": (*proof.blocking_items, exc.code),
                    }
                )
            except OSError:
                proof = proof.model_copy(
                    update={
                        "status": "blocked",
                        "blocking_items": (*proof.blocking_items, "source_input_unavailable"),
                    }
                )
        return proof

    def _validate_work_plan_handoff(
        self, plan: PlanRevisionView, *, verify_files: bool = True
    ) -> None:
        context = plan.plan.get("work_context")
        raw = context.get("analysis_handoff") if isinstance(context, dict) else None
        if plan.plan.get("kind") != "statistical_design" or not isinstance(raw, dict):
            return
        frozen = WorkAnalysisHandoff.model_validate(raw)
        current = self.prepare_work_analysis_handoff(
            str(frozen.data_references["source_run_id"]),
            plan.project_id,
            verify_files=verify_files,
        )
        require_same_handoff(frozen, current)

    def prepare_work_preprocessing_handoff(
        self,
        *,
        conversation_id: str,
        project_id: str,
        required_outputs: tuple[str, ...],
        reason: str,
        run_id: str | None = None,
        external_import_id: str | None = None,
    ) -> WorkPreprocessingHandoff:
        conversation = self.conversation_repository.get_conversation(conversation_id)
        if conversation.project_id != project_id:
            raise ConflictError("work_handoff_project_mismatch", "Handoff project changed.")
        if run_id and external_import_id:
            raise ConflictError("work_handoff_source_ambiguous", "Select one source.")
        outputs = preprocessing_output_requirements(required_outputs)
        blockers: list[str] = []
        project = self.project_repository.get_project(project_id)
        references: dict[str, Any] = {
            "project_id": project_id,
            "project_source_scope_hash": content_hash(project.source_roots),
            "source_run_id": run_id,
            "external_import_id": external_import_id,
            "input_selection_required": run_id is None,
        }
        if run_id:
            proof = self.prepare_work_analysis_handoff(run_id, project_id)
            references.update(proof.data_references)
            references["source_evidence_status"] = proof.status
            references["source_blocking_items"] = list(proof.blocking_items)
            references["input_selection_required"] = not bool(
                proof.data_references.get("dataset_id")
            )
        elif external_import_id:
            imported = self.external_import_repository.get_external_analysis_import(
                external_import_id
            )
            if imported.project_id != project_id:
                raise ConflictError(
                    "work_handoff_project_mismatch", "External source project changed."
                )
            references["external_import_hash"] = imported.inclusion_hash
            references["external_inspection_hash"] = content_hash(
                imported.inspection.model_dump(mode="json")
            )
            references["external_import_status"] = imported.status
            references["external_source_evidence"] = {
                "inspection": imported.inspection.model_dump(mode="json"),
                "inclusion": imported.inclusion.model_dump(mode="json")
                if imported.inclusion
                else None,
                "selection": imported.selection.model_dump(mode="json")
                if imported.selection
                else None,
            }
            try:
                source = self.path_policy.validate_read_path(
                    imported.source_path, project_roots=project.source_roots, expect_directory=True
                )
                current = inspect_external_metric_directory(
                    source, layout=imported.inspection.layout
                )
                if current.source_tree_hash != imported.inspection.source_tree_hash:
                    raise ConflictError("work_handoff_stale", "External source files changed.")
                inclusion = imported.inclusion
                if inclusion and inclusion.mask_relative_path:
                    mask_hash, mask_size, mask_grid = inspect_external_mask(
                        source, inclusion.mask_relative_path
                    )
                    if (mask_hash, mask_size, mask_grid) != (
                        inclusion.mask_sha256,
                        inclusion.mask_size_bytes,
                        inclusion.mask_grid_signature,
                    ):
                        raise ConflictError("work_handoff_stale", "External mask changed.")
            except ApplicationError as exc:
                blockers.append(exc.code)
            except (OSError, ValueError):
                blockers.append("work_handoff_source_unavailable")
        digest = preprocessing_handoff_hash(references, outputs)
        # Count unique requests only. Receipts, retransmissions and model wording
        # do not spend another handoff; another conversation starts a new chain.
        seen = set()
        for message in conversation.messages:
            handoff = message.payload.get("agent_handoff")
            if (
                not isinstance(handoff, dict)
                or handoff.get("from_agent") != "analysis"
                or handoff.get("status") not in {"pending", "received", "proposed"}
            ):
                continue
            refs = handoff.get("data_references")
            if isinstance(refs, dict) and refs.get("project_id") == project_id:
                seen.add(handoff.get("handoff_id"))
        limited = digest[:24] not in seen and len(seen) >= self.settings.work_max_handoffs
        if limited:
            blockers.append("work_handoff_limit_reached")
        return WorkPreprocessingHandoff(
            handoff_id=digest[:24],
            evidence_hash=digest,
            required_outputs=outputs,
            reason=reason[:1000],
            data_references=references,
            status="blocked" if blockers else "pending",
            blocking_items=tuple(blockers),
        )

    def _statistical_work_context(
        self, request: StatisticalDesignCreate, *, verify_source_files: bool = False
    ) -> dict[str, Any] | None:
        if request.work_context is None:
            return None
        from neuroagent.application.work_tasks import require_analysis_task

        conversation = self.conversation_repository.get_conversation(
            request.work_context.conversation_id
        )
        task = require_analysis_task(
            conversation,
            project_id=request.project_id,
            proposal_hash=request.work_context.proposal_hash,
        )
        message = next(
            (item for item in conversation.messages if item.message_id == task.proposal_message_id),
            None,
        )
        proposal = StatisticalDesignProposal.model_validate(
            message.payload.get("statistical_design_proposal") if message else None
        )
        if (
            proposal.proposal_hash != request.work_context.proposal_hash
            or proposal.project_id != request.project_id
            or proposal.project_version != request.expected_project_version
            or proposal.input_manifest_hash != request.input_manifest_hash
            or proposal.design != request.design
            or proposal.correction != request.correction
        ):
            raise ConflictError(
                "statistics_work_proposal_mismatch",
                "统计请求与已审查的服务端提案不一致，请重新生成方案。",
            )
        context: dict[str, Any] = {
            **request.work_context.model_dump(mode="json"),
            "proposal_message_id": task.proposal_message_id,
            "draft_hash": task.draft_hash,
        }
        if proposal.source_run_id is not None:
            current = self.prepare_work_analysis_handoff(
                proposal.source_run_id, request.project_id, verify_files=verify_source_files
            )
            raw = message.payload.get("agent_handoff") if message else None
            frozen = WorkAnalysisHandoff.model_validate(raw) if raw else current
            received = require_same_handoff(frozen, current)
            context["analysis_handoff"] = received.model_dump(mode="json")
        return context

    def create_statistical_design(
        self, request: StatisticalDesignCreate, idempotency_key: str
    ) -> StatisticalDesignView:
        def require_unbound_work_proposal() -> None:
            if request.work_context is None:
                return
            from neuroagent.application.work_tasks import require_analysis_task

            task = require_analysis_task(
                self.conversation_repository.get_conversation(request.work_context.conversation_id),
                project_id=request.project_id,
                proposal_hash=request.work_context.proposal_hash,
            )
            if task.plan_revision_id is not None:
                raise ConflictError(
                    "statistics_work_plan_already_created",
                    "当前统计提案已有正式方案，请继续使用该方案。",
                    plan_revision_id=task.plan_revision_id,
                )

        def prepare() -> tuple[tuple[tuple[float, ...], ...], dict[str, Any], str]:
            require_unbound_work_proposal()
            self._require_project_version(request.project_id, request.expected_project_version)
            work_context = self._statistical_work_context(request, verify_source_files=True)
            environment = self.environment_provider.current().snapshot
            image_artifact_ids = tuple(image.artifact_id for image in request.design.images)
            if request.design.qc_source is StatisticalQcSource.EXTERNAL_IMPORT:
                self._validate_external_statistical_source(request, verify_source_files=True)
            else:
                source_run_id = self._validate_statistics_qc_source(request, image_artifact_ids)
                self.artifact_repository.assert_artifacts_belong_to_run(
                    (*image_artifact_ids, request.design.mask_artifact_id),
                    source_run_id,
                )
                self._validate_statistical_artifact_lineage(
                    request.design,
                    manifest_hash=request.input_manifest_hash,
                    source_run_id=source_run_id,
                )
            self._validate_correction(request.design, request.correction)
            self._require_skill_parameter_schema(
                "plan-rsfmri-statistics",
                {
                    **request.design.model_dump(mode="json"),
                    "correction": (
                        None
                        if request.correction is None
                        else request.correction.model_dump(mode="json")
                    ),
                },
            )
            matrix = design_matrix(request.design)
            skill_locks, tool_locks = self._statistics_execution_locks(request.correction)
            plan_payload = {
                "kind": "statistical_design",
                "source_agent_role": "analysis",
                "design": request.design.model_dump(mode="json"),
                "correction": (
                    request.correction.model_dump(mode="json")
                    if request.correction is not None
                    else None
                ),
                "design_matrix": [list(row) for row in matrix],
                "skill_locks": skill_locks,
                "tool_locks": tool_locks,
            }
            if work_context is not None:
                plan_payload["work_context"] = work_context
            if request.atlas_id is not None:
                plan_payload["localization_plan"] = self._freeze_statistical_atlas(
                    request.atlas_id
                ).model_dump(mode="json")
            return matrix, plan_payload, environment.environment_hash

        def finalize(
            prepared: tuple[tuple[tuple[float, ...], ...], dict[str, Any], str],
        ) -> StatisticalDesignView:
            matrix, plan_payload, environment_hash = prepared
            if request.atlas_id is not None and self._freeze_statistical_atlas(
                request.atlas_id
            ).model_dump(mode="json") != plan_payload.get("localization_plan"):
                raise ConflictError("statistics_atlas_changed", "图谱在方案准备期间发生变化。")
            if self._statistical_work_context(request) != plan_payload.get("work_context"):
                raise ConflictError(
                    "statistics_work_proposal_stale", "统计草稿在准备期间发生变化，请重新审查。"
                )
            require_unbound_work_proposal()
            lock = {
                "plan": plan_payload,
                "manifest_hash": request.input_manifest_hash,
                "environment_hash": environment_hash,
            }
            plan = self.plan_repository.create_plan(
                project_id=request.project_id,
                expected_project_version=request.expected_project_version,
                plan=plan_payload,
                plan_hash=content_hash(lock),
                manifest_hash=request.input_manifest_hash,
                environment_hash=environment_hash,
                supersedes_id=request.supersedes_plan_revision_id,
            )
            if request.work_context is not None:
                self.conversation_repository.bind_statistics_task(
                    conversation_id=request.work_context.conversation_id,
                    project_id=request.project_id,
                    proposal_hash=request.work_context.proposal_hash,
                    plan_revision_id=plan.plan_revision_id,
                )
                work_task_id = ensure_statistical_work_task_for_plan(
                    self.automation,
                    self.conversation_repository.get_conversation,
                    plan,
                )
                if work_task_id is None:
                    raise ConflictError(
                        "statistics_work_task_missing",
                        "统计 Work 方案未能绑定持久分析任务，请重新审查方案。",
                    )
                self.conversation_repository.bind_statistics_task(
                    conversation_id=request.work_context.conversation_id,
                    project_id=request.project_id,
                    proposal_hash=request.work_context.proposal_hash,
                    plan_revision_id=plan.plan_revision_id,
                    work_task_id=work_task_id,
                )
            self.event_store.append_event(
                project_id=request.project_id,
                run_id=None,
                event_type="StatisticalDesignCreated",
                severity="info",
                payload={
                    "plan_revision_id": plan.plan_revision_id,
                    "plan_hash": plan.plan_hash,
                    "test": request.design.test.value,
                    "correction": (
                        request.correction.method if request.correction is not None else None
                    ),
                },
            )
            return StatisticalDesignView(
                design=request.design,
                localization_plan=plan_payload.get("localization_plan"),
                correction=request.correction,
                design_matrix=matrix,
                plan_revision=plan,
            )

        return self._idempotent_prepared(
            scope=f"projects:{request.project_id}:statistical-designs:create",
            key=idempotency_key,
            request=request,
            response_type=StatisticalDesignView,
            prepare=prepare,
            finalize=finalize,
        )

    def prepare_statistical_design_proposal(
        self,
        *,
        run_id: str,
        draft: StatisticsDraft,
        covariate_values: Mapping[str, Mapping[str, float | None]] | None = None,
        label_values: Mapping[str, Mapping[str, str | None]] | None = None,
    ) -> tuple[StatisticalDesignProposal | None, tuple[DesignIssue, ...]]:
        """Assemble a reviewable design from a run's frozen QC decision.

        The conversation never supplies identifiers: this method reads the run's
        approval state, the QC inclusion order and the registered artifacts, then
        hands them to the pure assembler.  Everything the model chose is method
        and column names only.

        Categorical labels (grouping, conditions) and numeric covariates are
        separate arguments on purpose: a grouping column is a string label, while
        a covariate is a number, and conflating them would let a numeric column be
        used as a group code.
        """

        run = self.run_repository.get_run(run_id)
        if run.state.value != "succeeded":
            return None, (
                _design_issue(
                    "source_run_not_succeeded",
                    f"运行当前状态为 {run.state.value}。统计设计必须在已成功完成的运行上建立。",
                ),
            )

        binding, included, allowed_metric_ids, manifest_hash, issue = (
            self._statistics_source_binding(run_id)
        )
        if issue is not None:
            return None, (issue,)

        artifacts_by_id = {
            item.artifact_id: item for item in self.artifact_repository.list_artifacts(run_id)
        }
        refs: list[ArtifactRef] = []
        for artifact_id, artifact in artifacts_by_id.items():
            if allowed_metric_ids is not None and artifact_id not in allowed_metric_ids:
                continue
            refs.append(_artifact_ref(artifact))
        mask_refs = [
            _artifact_ref(artifact)
            for artifact in artifacts_by_id.values()
            if artifact.artifact_type == (draft.mask_artifact_type or _BRAIN_MASK_TYPE)
        ]

        sources, source_issues = collect_design_sources(
            artifacts=(*refs, *mask_refs),
            included_subject_ids=included,
            metric_artifact_type=draft.metric_artifact_type or "",
            mask_artifact_type=draft.mask_artifact_type or _BRAIN_MASK_TYPE,
            label_column=draft.grouping_column,
            label_values=(
                (label_values or {}).get(draft.grouping_column, {})
                if draft.grouping_column
                else None
            ),
        )
        if sources is None:
            return None, source_issues

        design, design_issues = assemble_statistical_design(
            choices=_choices_from_draft(draft),
            subjects=sources.subjects,
            covariate_values=covariate_values or {},
            mask_artifact_id=sources.mask_artifact_id,
            revision_id=f"conversation-{run_id}-{statistics_draft_hash(draft)[:12]}",
            qc=binding,
        )
        if design is None:
            return None, design_issues

        correction, correction_issues = build_correction(draft, design)
        if correction_issues:
            return None, correction_issues
        try:
            validate_correction_for_design(design, correction)
        except ValueError as exc:
            return None, (_design_issue("correction_rejected_by_domain", str(exc)),)

        project = self.project_repository.get_project(run.project_id)
        return (
            StatisticalDesignProposal(
                design=design,
                correction=correction,
                project_id=run.project_id,
                project_version=project.version,
                input_manifest_hash=manifest_hash,
                source_run_id=run_id,
                frozen_subject_order=design.subject_order,
                metric_artifact_ids=tuple(image.artifact_id for image in design.images),
                review=render_statistics_draft_review(draft),
                proposal_hash=proposal_hash(design, correction),
            ),
            (),
        )

    def prepare_work_statistical_design_proposal(
        self,
        *,
        run_id: str | None = None,
        draft: StatisticsDraft,
        external_import_id: str | None = None,
        project_id: str | None = None,
    ) -> tuple[StatisticalDesignProposal | None, tuple[DesignIssue, ...]]:
        """Bind a Work statistics draft to registered run and demographics evidence."""

        if draft.test is not None:
            capability = next(
                (item for item in STATISTICAL_CAPABILITIES if item["test"] == draft.test.value),
                None,
            )
            if capability is None or not capability["executable"]:
                return None, (
                    _design_issue(
                        "statistics_capability_not_validated",
                        "该统计适配尚未通过本机 DPABI 验证，暂不能生成可执行方案。",
                    ),
                )

        if external_import_id is not None:
            if run_id is not None or project_id is None:
                return None, (
                    _design_issue(
                        "ambiguous_statistics_source", "请选择一个明确的统计来源及所属项目。"
                    ),
                )
            return self.prepare_external_statistical_design_proposal(
                external_import_id=external_import_id, project_id=project_id, draft=draft
            )
        if run_id is None:
            return None, (_design_issue("statistics_source_missing", "请先选择统计来源。"),)
        run = self.run_repository.get_run(run_id)
        source_plan = self.plan_repository.get_plan(run.plan_revision_id)
        manifest_id = source_plan.plan.get("dataset_manifest_id")
        if not isinstance(manifest_id, str) or not manifest_id:
            return None, (
                _design_issue(
                    "source_manifest_unavailable",
                    "来源运行没有已登记的数据集 manifest，不能建立统计设计。",
                ),
            )
        manifest = self.project_repository.get_manifest(manifest_id)

        needs_demographics = bool(draft.grouping_column or draft.covariates)
        demographics = (
            self.project_repository.get_latest_demographics_for_dataset(manifest.dataset_id)
            if needs_demographics
            else None
        )
        if needs_demographics and demographics is None:
            return None, (
                _design_issue(
                    "demographics_revision_missing",
                    "当前数据集没有已登记且与受试者对齐的人口学 revision；分组与协变量无法绑定。",
                ),
            )

        label_values: dict[str, dict[str, str | None]] = {}
        covariate_values: dict[str, dict[str, float | None]] = {}
        demographics_hash: str | None = None
        demographics_id: str | None = None
        if demographics is not None:
            demographics_id = demographics.demographics_id
            demographics_hash = demographics.content_hash
            demographics_dataset_id, content = self.project_repository.get_demographics_content(
                demographics.demographics_id
            )
            if demographics_dataset_id != manifest.dataset_id:
                return None, (
                    _design_issue(
                        "demographics_dataset_mismatch",
                        "人口学 revision 不属于来源运行对应的数据集，统计设计被阻断。",
                    ),
                )
            rows = content.get("rows", [])
            if not isinstance(rows, list):
                return None, (
                    _design_issue("demographics_rows_invalid", "人口学 revision 行结构无效。"),
                )
            by_subject = {
                str(row.get("subject_id")): row
                for row in rows
                if isinstance(row, dict) and row.get("subject_id") is not None
            }
            if draft.grouping_column:
                label_values[draft.grouping_column] = {
                    subject_id: (
                        str(by_subject[subject_id][draft.grouping_column])
                        if subject_id in by_subject
                        and by_subject[subject_id].get(draft.grouping_column) not in (None, "")
                        else None
                    )
                    for subject_id in (entry.subject_id for entry in manifest.subjects)
                }
            for choice in draft.covariates:
                values: dict[str, float | None] = {}
                for subject_id, row in by_subject.items():
                    raw_value = row.get(choice.name)
                    try:
                        values[subject_id] = (
                            None if raw_value in (None, "") else float(str(raw_value))
                        )
                    except (TypeError, ValueError):
                        values[subject_id] = None
                covariate_values[choice.name] = values

        proposal, issues = self.prepare_statistical_design_proposal(
            run_id=run_id,
            draft=draft,
            label_values=label_values,
            covariate_values=covariate_values,
        )
        if proposal is not None:
            proposal = proposal.model_copy(
                update={
                    "demographics_revision_id": demographics_id,
                    "demographics_hash": demographics_hash,
                    "proposal_hash": proposal_hash(
                        proposal.design,
                        proposal.correction,
                        demographics_revision_id=demographics_id,
                        demographics_hash=demographics_hash,
                    ),
                }
            )
        return proposal, issues

    def prepare_external_statistical_design_proposal(
        self, *, external_import_id: str, project_id: str, draft: StatisticsDraft
    ) -> tuple[StatisticalDesignProposal | None, tuple[DesignIssue, ...]]:
        """Bind method choices to an explicitly selected, independently reviewed import."""
        capability = next(
            (
                item
                for item in STATISTICAL_CAPABILITIES
                if draft.test is not None and item["test"] == draft.test.value
            ),
            None,
        )
        if capability is None or not capability["executable"]:
            return None, (
                _design_issue(
                    "statistics_capability_not_validated", "该统计适配尚未通过本机 DPABI 验证。"
                ),
            )
        imported = self.external_import_repository.get_external_analysis_import(external_import_id)
        if imported.project_id != project_id:
            return None, (
                _design_issue("cross_project_external_import", "外部分析输入不属于当前项目。"),
            )
        selection = imported.selection
        if (
            imported.status != "included"
            or selection is None
            or not imported.inclusion_hash
            or imported.inspection.metric_kind is None
            or imported.inspection.scaling is None
        ):
            return None, (
                _design_issue(
                    "external_import_not_selectable", "请先确认外部数据纳入，并登记同网格掩膜。"
                ),
            )
        if draft.test is None or draft.tail is None or draft.missing_value_policy is None:
            return None, (
                _design_issue(
                    "external_method_choices_missing", "请明确检验类型、尾部和缺失值处理。"
                ),
            )
        paired = draft.test is StatisticalTest.PAIRED_T
        if paired != (imported.inspection.layout == "paired_conditions"):
            return None, (
                _design_issue(
                    "external_import_layout_mismatch",
                    "检验类型与已确认的分组或配对条件布局不一致。",
                ),
            )
        if paired and (
            draft.condition_column != "condition"
            or set(draft.condition_order) != set(selection.group_order)
        ):
            return None, (
                _design_issue(
                    "external_import_condition_mismatch",
                    "配对条件列为 condition，条件顺序须明确引用两个已确认标签。",
                ),
            )
        grouped = draft.test in {
            StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
            StatisticalTest.ONE_WAY_ANOVA_ANCOVA,
        }
        if grouped and draft.grouping_column != "group":
            return None, (
                _design_issue(
                    "external_group_column_invalid", "当前外部输入的已登记分组列为 group。"
                ),
            )
        metric_type = (
            f"external.{imported.inspection.metric_kind.value}.{imported.inspection.scaling.value}"
        )
        if draft.metric_artifact_type != metric_type:
            return None, (
                _design_issue(
                    "external_metric_type_mismatch", "指标类型必须引用当前外部输入的已登记类型。"
                ),
            )
        demographics = (
            self.external_import_repository.get_latest_external_demographics(imported.import_id)
            if draft.covariates
            else None
        )
        covariate_values: dict[str, dict[str, float | None]] = {}
        if draft.covariates:
            if demographics is None:
                return None, (
                    _design_issue("external_demographics_required", "请先登记并对齐外部人口学表。"),
                )
            _, content = self.external_import_repository.get_external_demographics_content(
                demographics.demographics_id
            )
            for choice in draft.covariates:
                if choice.name not in demographics.columns:
                    return None, (
                        _design_issue("covariate_column_missing", "所选协变量列未登记。"),
                    )
                values: dict[str, float | None] = {}
                for row in content["rows"]:
                    try:
                        numeric = float(row[choice.name])
                        values[str(row["subject_id"])] = numeric if math.isfinite(numeric) else None
                    except (TypeError, ValueError):
                        values[str(row["subject_id"])] = None
                covariate_values[choice.name] = values
        design, issues = assemble_statistical_design(
            choices=_choices_from_draft(draft),
            subjects=tuple(
                DesignSubject(
                    subject_id=image.subject_id,
                    artifact_id=image.artifact_id,
                    group=image.group if grouped else None,
                    condition=image.condition,
                )
                for image in selection.images
            ),
            covariate_values=covariate_values,
            mask_artifact_id=selection.mask_artifact_id,
            revision_id=f"external-{imported.import_id}-{statistics_draft_hash(draft)[:12]}",
            qc=QcBinding(
                source=StatisticalQcSource.EXTERNAL_IMPORT,
                external_import_id=imported.import_id,
                external_import_hash=imported.inclusion_hash,
            ),
        )
        if design is None:
            return None, issues
        if demographics is not None:
            design = StatisticalDesignRevision.model_validate(
                {
                    **design.model_dump(mode="python"),
                    "external_demographics_revision_id": demographics.demographics_id,
                    "external_demographics_hash": demographics.content_hash,
                }
            )
        correction, issues = build_correction(draft, design)
        if issues:
            return None, issues
        project = self.project_repository.get_project(project_id)
        try:
            validate_correction_for_design(design, correction)
            self._validate_external_statistical_source(
                StatisticalDesignCreate(
                    project_id=project_id,
                    expected_project_version=project.version,
                    input_manifest_hash=selection.input_manifest_hash,
                    design=design,
                    correction=correction,
                ),
                verify_source_files=True,
            )
        except ApplicationError as exc:
            return None, (_design_issue(exc.code, str(exc)),)
        except ValueError as exc:
            return None, (_design_issue("external_design_rejected", str(exc)),)
        return StatisticalDesignProposal(
            design=design,
            correction=correction,
            project_id=project_id,
            project_version=project.version,
            input_manifest_hash=selection.input_manifest_hash,
            frozen_subject_order=design.subject_order,
            metric_artifact_ids=tuple(image.artifact_id for image in design.images),
            demographics_revision_id=demographics.demographics_id if demographics else None,
            demographics_hash=demographics.content_hash if demographics else None,
            review=render_statistics_draft_review(draft)
            + "\n- 来源：已确认的外部纳入记录与掩膜；未知处理信息仍保持未知。",
            proposal_hash=proposal_hash(
                design,
                correction,
                demographics_revision_id=demographics.demographics_id if demographics else None,
                demographics_hash=demographics.content_hash if demographics else None,
            ),
        ), ()

    def _statistics_source_binding(
        self, run_id: str
    ) -> tuple[QcBinding, tuple[str, ...], frozenset[str] | None, str, DesignIssue | None]:
        """Resolve the QC binding, frozen inclusion order and manifest to use.

        An approved QC review is preferred, because its inclusion order and metric
        artifact list are the frozen scientific decision.  A recorded QC skip is
        accepted, but then there is no reviewed artifact list to inherit, so every
        registered metric artifact of the run is eligible.
        """

        review = self.qc_repository.get_latest_qc_review_for_run(run_id)
        if review is not None and review.state.value == "approved":
            return (
                QcBinding(
                    source=StatisticalQcSource.APPROVED_REVIEW,
                    qc_review_revision_id=review.review.review_revision_id,
                    qc_review_hash=review.review.content_hash,
                ),
                tuple(review.review.included_subject_ids),
                frozenset(review.review.metric_artifact_ids),
                review.review.input_manifest_hash,
                None,
            )

        skip = self.qc_repository.get_qc_skip_for_run(run_id)
        if skip is not None:
            run = self.run_repository.get_run(run_id)
            plan = self.plan_repository.get_plan(run.plan_revision_id)
            approved = (
                self.qc_repository.get_approved_qc_review(skip.qc_review_revision_id)
                if skip.qc_review_revision_id
                else None
            )
            included = tuple(approved.review.included_subject_ids) if approved is not None else ()
            if not included:
                return (
                    QcBinding(source=StatisticalQcSource.SKIPPED),
                    (),
                    None,
                    plan.manifest_hash,
                    _design_issue(
                        "qc_inclusion_order_unavailable",
                        "该运行的 QC 跳过记录没有可继承的受试者纳入顺序。"
                        "无法建立显式对齐的统计设计。",
                    ),
                )
            return (
                QcBinding(
                    source=StatisticalQcSource.SKIPPED,
                    qc_skip_id=skip.qc_skip_id,
                    qc_skip_hash=skip.decision_hash,
                    qc_source_run_id=run_id,
                ),
                included,
                None,
                plan.manifest_hash,
                None,
            )

        return (
            QcBinding(source=StatisticalQcSource.APPROVED_REVIEW),
            (),
            None,
            "",
            _design_issue(
                "statistics_source_unapproved",
                "该运行既无已批准的 QC review 也无 QC 跳过记录。统计设计必须建立在其中之一之上。",
            ),
        )

    def _validate_statistics_qc_source(
        self,
        request: StatisticalDesignCreate,
        image_artifact_ids: tuple[str, ...],
    ) -> str:
        design = request.design
        if design.qc_source is StatisticalQcSource.APPROVED_REVIEW:
            assert design.qc_review_revision_id is not None and design.qc_review_hash is not None
            qc_review = self.qc_repository.get_approved_qc_review(design.qc_review_revision_id)
            if qc_review.project_id != request.project_id:
                raise ConflictError("cross_project_qc_review", "QC review 不属于当前项目。")
            if qc_review.review.content_hash != design.qc_review_hash:
                raise ConflictError(
                    "qc_review_hash_mismatch", "统计设计引用的 QC review hash 与已批准版本不一致。"
                )
            if qc_review.review.input_manifest_hash != request.input_manifest_hash:
                raise ConflictError(
                    "qc_manifest_hash_mismatch", "统计设计输入清单与已批准 QC review 不一致。"
                )
            try:
                assert_statistics_ready(qc_review.review, design.subject_order)
            except ValueError as exc:
                raise InputValidationError(
                    "statistics_qc_alignment_failed", "统计受试者顺序必须精确等于 QC 冻结纳入顺序。"
                ) from exc
            if not set(image_artifact_ids).issubset(qc_review.review.metric_artifact_ids):
                raise InputValidationError(
                    "statistics_artifact_not_qc_approved",
                    "统计影像必须全部来自 QC review 冻结的指标 Artifact 清单。",
                )
            return qc_review.run_id

        assert (
            design.qc_source_run_id is not None
            and design.qc_skip_id is not None
            and design.qc_skip_hash is not None
        )
        skip = self.qc_repository.get_qc_skip_for_run(design.qc_source_run_id)
        if (
            skip is None
            or skip.qc_skip_id != design.qc_skip_id
            or skip.decision_hash != design.qc_skip_hash
        ):
            raise ConflictError(
                "qc_skip_binding_mismatch", "统计设计引用的 QC 跳过记录无效或已失配。"
            )
        source_run = self.run_repository.get_run(design.qc_source_run_id)
        if source_run.project_id != request.project_id or source_run.state.value != "succeeded":
            raise ConflictError(
                "qc_skip_source_run_invalid", "QC 跳过来源运行不属于当前项目或尚未成功完成。"
            )
        source_plan = self.plan_repository.get_plan(source_run.plan_revision_id)
        if source_plan.manifest_hash != request.input_manifest_hash:
            raise ConflictError(
                "qc_skip_manifest_hash_mismatch", "QC 跳过来源运行的输入清单不匹配。"
            )
        return source_run.run_id

    def get_statistical_design(self, plan_revision_id: str) -> StatisticalDesignView:
        return self._statistics_view(self.plan_repository.get_plan(plan_revision_id))

    def validate_statistical_design(
        self,
        plan_revision_id: str,
        request: StatisticalDesignValidationRequest,
        idempotency_key: str,
    ) -> StatisticalDesignView:
        def prepare() -> None:
            plan = self.plan_repository.get_plan(plan_revision_id)
            self._validate_work_plan_handoff(plan)
            self._validate_statistical_atlas(plan)

        def finalize(_prepared: None) -> StatisticalDesignView:
            current = self.plan_repository.get_plan(plan_revision_id)
            self._validate_statistical_atlas(current)
            self._validate_work_plan_handoff(current, verify_files=False)
            self._statistics_view(current)
            from neuroagent.application.work_tasks import require_statistical_work_plan

            require_statistical_work_plan(self.conversation_repository.get_conversation, current)
            validated = self.plan_repository.validate_plan(
                plan_revision_id,
                expected_version=request.expected_version,
                issues=[],
            )
            self.event_store.append_event(
                project_id=validated.project_id,
                run_id=None,
                event_type="StatisticalDesignValidated",
                severity="info",
                payload={
                    "plan_revision_id": validated.plan_revision_id,
                    "plan_hash": validated.plan_hash,
                },
            )
            return self._statistics_view(validated)

        return self._idempotent_prepared(
            scope=f"statistical-designs:{plan_revision_id}:validate",
            key=idempotency_key,
            request=request,
            response_type=StatisticalDesignView,
            prepare=prepare,
            finalize=finalize,
        )

    @staticmethod
    def correction_capabilities() -> list[CorrectionCapabilityView]:
        return [
            CorrectionCapabilityView.model_validate(
                {
                    "method": "fdr",
                    "skill_id": "rsfmri.statistics.fdr",
                    "description": "FDR correction with an explicit q threshold and mask.",
                    "schema": FdrCorrection.model_json_schema(),
                }
            ),
            CorrectionCapabilityView.model_validate(
                {
                    "method": "grf",
                    "skill_id": "rsfmri.statistics.grf",
                    "description": (
                        "GRF correction with explicit voxel, cluster, tail, and mask settings."
                    ),
                    "schema": GrfCorrection.model_json_schema(),
                }
            ),
        ]

    def _plan_staleness_reasons(
        self,
        plan: PlanRevisionView,
        *,
        current_environment_hash: str,
        verify_source_files: bool = True,
        seen: set[str] | None = None,
    ) -> list[dict[str, str]]:
        kind = plan.plan.get("kind")
        if kind is None:
            # Legacy/internal mock plans are retained for lightweight executor tests.
            return []
        reasons: list[dict[str, str]] = []
        if kind not in {"skill_plan", "statistical_design"}:
            return [{"code": "unsupported_plan_kind", "message": str(kind)}]
        if plan.environment_hash != current_environment_hash:
            reasons.append(
                {
                    "code": "environment_lock_changed",
                    "message": "MATLAB/SPM/DPABI/adapter environment lock changed",
                }
            )
        if kind == "skill_plan":
            return self._skill_plan_staleness_reasons(plan, reasons, verify_source_files)
        return self._statistical_design_staleness_reasons(
            plan,
            reasons,
            current_environment_hash=current_environment_hash,
            verify_source_files=verify_source_files,
            seen=seen,
        )

    def _skill_plan_staleness_reasons(
        self,
        plan: PlanRevisionView,
        reasons: list[dict[str, str]],
        verify_source_files: bool,
    ) -> list[dict[str, str]]:
        try:
            skill_plan = SkillPlan.model_validate(plan.plan.get("skill_plan"))
        except ValidationError:
            return [
                *reasons,
                {"code": "skill_plan_invalid", "message": "stored SkillPlan is invalid"},
            ]
        if skill_plan.plan_hash != plan.plan_hash:
            reasons.append(
                {
                    "code": "skill_plan_hash_changed",
                    "message": "embedded SkillPlan hash differs from the approved revision",
                }
            )
        if (
            skill_plan.input_manifest_hash != plan.manifest_hash
            or skill_plan.environment.environment_hash != plan.environment_hash
        ):
            reasons.append(
                {
                    "code": "skill_plan_lock_mismatch",
                    "message": "stored SkillPlan locks differ from revision locks",
                }
            )
        reasons.extend(self._dataset_lock_staleness_reasons(plan, skill_plan, verify_source_files))
        reasons.extend(self._skill_and_tool_lock_staleness_reasons(skill_plan))
        return reasons

    def _dataset_lock_staleness_reasons(
        self,
        plan: PlanRevisionView,
        skill_plan: SkillPlan,
        verify_source_files: bool,
    ) -> list[dict[str, str]]:
        reasons: list[dict[str, str]] = []
        try:
            dataset = self.project_repository.get_dataset(skill_plan.dataset_ref)
            manifest_id = plan.plan.get("dataset_manifest_id")
            if dataset.project_id != plan.project_id:
                reasons.append(
                    {
                        "code": "dataset_project_changed",
                        "message": "dataset no longer belongs to the approved project",
                    }
                )
            if not isinstance(manifest_id, str) or dataset.current_manifest_id != manifest_id:
                reasons.append(
                    {
                        "code": "dataset_manifest_revision_changed",
                        "message": "dataset current manifest revision changed",
                    }
                )
            current_manifest = None
            if isinstance(manifest_id, str) and dataset.current_manifest_id == manifest_id:
                current_manifest = self.project_repository.get_manifest(manifest_id)
            if current_manifest is not None and current_manifest.content_hash != plan.manifest_hash:
                reasons.append(
                    {
                        "code": "dataset_manifest_hash_changed",
                        "message": "dataset current manifest hash changed",
                    }
                )
            if verify_source_files:
                project = self.project_repository.get_project(dataset.project_id)
                source = self.path_policy.validate_read_path(
                    dataset.source_path,
                    project_roots=project.source_roots,
                    expect_directory=True,
                )
                current_source_hash = content_hash(
                    self._inspect_source(
                        source,
                        report_only=False,
                        selected_input_stage=(
                            current_manifest.selected_input_stage if current_manifest else None
                        ),
                    )
                )
                if current_source_hash != plan.manifest_hash:
                    reasons.append(
                        {
                            "code": "source_manifest_content_changed",
                            "message": (
                                "dataset source files no longer match the approved manifest hash"
                            ),
                        }
                    )
        except (ApplicationError, OSError):
            reasons.append(
                {
                    "code": "dataset_lock_unavailable",
                    "message": "approved dataset or manifest lock is unavailable",
                }
            )
        return reasons

    def _skill_and_tool_lock_staleness_reasons(self, skill_plan: SkillPlan) -> list[dict[str, str]]:
        reasons: list[dict[str, str]] = []
        for lock in skill_plan.skill_locks:
            try:
                current = self.skill_registry.resolve(lock.skill_id, lock.version)
            except SkillRegistryError:
                reasons.append(
                    {
                        "code": "skill_lock_unavailable",
                        "message": f"{lock.skill_id}@{lock.version}",
                    }
                )
                continue
            if current.content_hash != lock.content_hash:
                reasons.append(
                    {
                        "code": "skill_lock_changed",
                        "message": f"{lock.skill_id}@{lock.version}",
                    }
                )
        for step in skill_plan.steps:
            try:
                current_tool = self.tool_registry.resolve_capability(step.tool.capability)
            except ToolRegistryError:
                reasons.append(
                    {
                        "code": "tool_lock_unavailable",
                        "message": step.tool.capability,
                    }
                )
                continue
            if current_tool != step.tool:
                reasons.append(
                    {
                        "code": "tool_lock_changed",
                        "message": step.tool.capability,
                    }
                )
        return reasons

    def _statistical_design_staleness_reasons(
        self,
        plan: PlanRevisionView,
        reasons: list[dict[str, str]],
        *,
        current_environment_hash: str,
        verify_source_files: bool,
        seen: set[str] | None,
    ) -> list[dict[str, str]]:
        _validate_statistical_locks(self, plan, reasons)
        try:
            self._validate_statistical_atlas(plan)
        except (ApplicationError, OSError, ValidationError, ValueError):
            reasons.append(
                {
                    "code": "statistics_atlas_changed",
                    "message": "所选图谱不可用或与冻结版本不一致，请重新审查方案。",
                }
            )
        try:
            work_context = plan.plan.get("work_context")
            handoff = (
                work_context.get("analysis_handoff") if isinstance(work_context, dict) else None
            )
            if isinstance(handoff, dict):
                frozen = WorkAnalysisHandoff.model_validate(handoff)
                current = self.prepare_work_analysis_handoff(
                    str(frozen.data_references["source_run_id"]),
                    plan.project_id,
                    verify_files=verify_source_files,
                )
                require_same_handoff(frozen, current)
            design_view = self._statistics_view(plan)
            design = design_view.design
            if design.qc_source is StatisticalQcSource.EXTERNAL_IMPORT:
                self._validate_external_statistical_source(
                    StatisticalDesignCreate(
                        project_id=plan.project_id,
                        expected_project_version=1,
                        input_manifest_hash=plan.manifest_hash,
                        design=design,
                        correction=design_view.correction,
                    ),
                    verify_source_files=verify_source_files,
                )
                return reasons
            if design.qc_source is StatisticalQcSource.APPROVED_REVIEW:
                assert (
                    design.qc_review_revision_id is not None and design.qc_review_hash is not None
                )
                qc_review = self.qc_repository.get_approved_qc_review(design.qc_review_revision_id)
                if (
                    qc_review.project_id != plan.project_id
                    or qc_review.review.content_hash != design.qc_review_hash
                    or qc_review.review.input_manifest_hash != plan.manifest_hash
                ):
                    reasons.append(
                        {
                            "code": "qc_lock_changed",
                            "message": "approved QC review no longer matches design",
                        }
                    )
                assert_statistics_ready(qc_review.review, design.subject_order)
                source_run = self.run_repository.get_run(qc_review.run_id)
            else:
                assert design.qc_source_run_id and design.qc_skip_id and design.qc_skip_hash
                skip = self.qc_repository.get_qc_skip_for_run(design.qc_source_run_id)
                if (
                    skip is None
                    or skip.qc_skip_id != design.qc_skip_id
                    or skip.decision_hash != design.qc_skip_hash
                ):
                    reasons.append(
                        {
                            "code": "qc_skip_lock_changed",
                            "message": "QC skip record no longer matches the statistical design",
                        }
                    )
                source_run = self.run_repository.get_run(design.qc_source_run_id)
            source_plan = self.plan_repository.get_plan(source_run.plan_revision_id)
            if source_plan.manifest_hash != plan.manifest_hash:
                reasons.append(
                    {
                        "code": "qc_source_manifest_changed",
                        "message": "QC source plan manifest differs from statistical input",
                    }
                )
            visited = set() if seen is None else set(seen)
            if plan.plan_revision_id not in visited:
                visited.add(plan.plan_revision_id)
                reasons.extend(
                    self._plan_staleness_reasons(
                        source_plan,
                        # A completed producer is evidence, not another job to
                        # execute under the consumer's environment. Preserve
                        # its historical lock while rechecking all other inputs.
                        current_environment_hash=(
                            source_plan.environment_hash
                            if source_run.state is WorkflowState.SUCCEEDED
                            else current_environment_hash
                        ),
                        verify_source_files=verify_source_files,
                        seen=visited,
                    )
                )
        except (ApplicationError, OSError, ValidationError, ValueError):
            reasons.append(
                {
                    "code": "qc_lock_unavailable",
                    "message": "approved QC lineage is unavailable or invalid",
                }
            )
        return reasons

    def _require_plan_current(
        self, plan: PlanRevisionView, *, verify_source_files: bool = True
    ) -> None:
        """Fail closed on execution locks; optionally reuse the just-completed source scan."""

        current_environment_hash = self.environment_provider.current().snapshot.environment_hash
        reasons = self._plan_staleness_reasons(
            plan,
            current_environment_hash=current_environment_hash,
            verify_source_files=verify_source_files,
        )
        if reasons:
            raise ConflictError(
                "plan_stale",
                "计划的 manifest、环境、Skill、Tool 或 QC 锁已变化, 请重新生成并审批。",
                reasons=reasons,
            )
