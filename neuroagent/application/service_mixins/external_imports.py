"""Inspection and explicit inclusion of externally produced DPABI metric maps."""

# ruff: noqa: RUF001

from __future__ import annotations

from typing import Any

from neuroagent.analysis.external_metric_import import (
    inspect_external_mask,
    inspect_external_metric_directory,
)
from neuroagent.application.contracts import (
    ExternalAnalysisImportIncludeRequest,
    ExternalAnalysisImportInspectRequest,
    ExternalAnalysisImportSummaryView,
    ExternalAnalysisImportView,
    ExternalDemographicsImportRequest,
    ExternalDemographicsRevisionView,
)
from neuroagent.application.errors import ConflictError, InputValidationError
from neuroagent.application.hashing import content_hash
from neuroagent.application.service_mixins._base import BaseServiceMixin
from neuroagent.domain.fmri.external_import import ExternalMetricInclusion


class ExternalAnalysisImportMixin(BaseServiceMixin):
    """Persist an independent scan and require a version-bound human decision."""

    def import_external_demographics(
        self, import_id: str, request: ExternalDemographicsImportRequest, idempotency_key: str
    ) -> ExternalDemographicsRevisionView:
        def prepare() -> dict[str, Any]:
            imported = self.repository.get_external_analysis_import(import_id)
            if imported.status != "included" or imported.inclusion is None:
                raise ConflictError("external_import_not_included", "请先确认外部影像纳入决定。")
            if imported.version != request.expected_import_version:
                raise ConflictError("external_import_version_mismatch", "请重新载入外部数据记录。")
            project = self.repository.get_project(imported.project_id)
            path = self.path_policy.validate_read_path(
                request.source_path, project_roots=project.source_roots, expect_directory=False
            )
            content = self.demographics_reader(
                path,
                subject_id_column=request.subject_id_column,
                column_mapping=request.column_mapping,
                encoding=request.encoding,
                manifest_subject_ids=set(imported.inclusion.included_subject_ids),
            )
            if content["missing_subject_ids"] or content["extra_subject_ids"]:
                raise InputValidationError(
                    "external_demographics_subject_mismatch",
                    "人口学表必须恰好覆盖已纳入受试者，请核对编号及纳入名单。",
                    missing_subject_ids=content["missing_subject_ids"],
                    extra_subject_ids=content["extra_subject_ids"],
                )
            content["inclusion_hash"] = imported.inclusion_hash
            return content

        def finalize(content: dict[str, Any]) -> ExternalDemographicsRevisionView:
            result = self.repository.create_external_demographics(
                import_id, expected_version=request.expected_import_version, content=content
            )
            imported = self.repository.get_external_analysis_import(import_id)
            self.repository.append_event(
                project_id=imported.project_id,
                run_id=None,
                event_type="ExternalDemographicsAligned",
                severity="info",
                payload={
                    "external_import_id": import_id,
                    "demographics_id": result.demographics_id,
                    "content_hash": result.content_hash,
                    "revision": result.revision,
                },
            )
            return result

        return self._idempotent_prepared(
            scope=f"external-imports:{import_id}:demographics:import",
            key=idempotency_key,
            request=request,
            response_type=ExternalDemographicsRevisionView,
            prepare=prepare,
            finalize=finalize,
        )

    def get_external_demographics(self, import_id: str) -> ExternalDemographicsRevisionView | None:
        self.repository.get_external_analysis_import(import_id)
        return self.repository.get_latest_external_demographics(import_id)

    def inspect_external_analysis_import(
        self, request: ExternalAnalysisImportInspectRequest, idempotency_key: str
    ) -> ExternalAnalysisImportView:
        def prepare() -> tuple[str, dict[str, Any]]:
            project = self.repository.get_project(request.project_id)
            source = self.path_policy.validate_read_path(
                request.source_path,
                project_roots=project.source_roots,
                expect_directory=True,
            )
            try:
                inspection = inspect_external_metric_directory(source, layout=request.layout)
            except (OSError, ValueError) as exc:
                raise InputValidationError(
                    "external_analysis_import_inspection_failed",
                    "外部 DPABI 指标目录检查失败。",
                    reason=f"{type(exc).__name__}: {exc}",
                ) from exc
            return str(source), inspection.model_dump(mode="json")

        def finalize(prepared: tuple[str, dict[str, Any]]) -> ExternalAnalysisImportView:
            source_path, inspection = prepared
            result = self.repository.create_external_analysis_import(
                project_id=request.project_id,
                source_path=source_path,
                inspection=inspection,
            )
            self.repository.append_event(
                project_id=request.project_id,
                run_id=None,
                event_type="ExternalAnalysisImportInspected",
                severity="info" if not result.inspection.blocking_issues else "warning",
                payload={
                    "import_id": result.import_id,
                    "status": result.status,
                    "source_tree_hash": result.inspection.source_tree_hash,
                    "metric_kind": (
                        result.inspection.metric_kind.value
                        if result.inspection.metric_kind is not None
                        else None
                    ),
                    "image_count": len(result.inspection.images),
                    "group_count": len(result.inspection.group_order),
                    "blocking_issue_count": len(result.inspection.blocking_issues),
                },
            )
            return result

        return self._idempotent_prepared(
            scope=f"projects:{request.project_id}:external-analysis-imports:inspect",
            key=idempotency_key,
            request=request,
            response_type=ExternalAnalysisImportView,
            prepare=prepare,
            finalize=finalize,
        )

    def list_external_analysis_imports(
        self, *, project_id: str
    ) -> list[ExternalAnalysisImportSummaryView]:
        return self.repository.list_external_analysis_imports(project_id=project_id)

    def get_external_analysis_import(self, import_id: str) -> ExternalAnalysisImportView:
        return self.repository.get_external_analysis_import(import_id)

    def include_external_analysis_import(
        self,
        import_id: str,
        request: ExternalAnalysisImportIncludeRequest,
        idempotency_key: str,
    ) -> ExternalAnalysisImportView:
        def prepare() -> dict[str, Any]:
            existing = self.repository.get_external_analysis_import(import_id)
            if existing.status != "needs_review":
                raise ConflictError(
                    "external_import_already_included",
                    "此外部分析目录已完成纳入确认。",
                    import_id=import_id,
                )
            if existing.version != request.expected_version:
                raise ConflictError(
                    "external_import_version_mismatch",
                    "外部分析目录版本已变化，请重新载入检查结果。",
                    expected=request.expected_version,
                    actual=existing.version,
                )
            project = self.repository.get_project(existing.project_id)
            source = self.path_policy.validate_read_path(
                existing.source_path,
                project_roots=project.source_roots,
                expect_directory=True,
            )
            try:
                current = inspect_external_metric_directory(
                    source, layout=existing.inspection.layout
                )
            except (OSError, ValueError) as exc:
                raise ConflictError(
                    "external_import_source_unavailable",
                    "纳入前无法重新检查外部指标目录。",
                    reason=f"{type(exc).__name__}: {exc}",
                ) from exc
            if current.source_tree_hash != existing.inspection.source_tree_hash:
                raise ConflictError(
                    "external_import_source_changed",
                    "外部指标文件在检查后发生变化；请重新导入并检查。",
                    expected_hash=existing.inspection.source_tree_hash,
                    current_hash=current.source_tree_hash,
                )
            if current.blocking_issues:
                raise InputValidationError(
                    "external_import_has_blocking_issues",
                    "检查结果仍包含阻断项，不能纳入分析数据。",
                    issues=list(current.blocking_issues),
                )
            if current.metric_kind is None or current.scaling is None:
                raise InputValidationError(
                    "external_import_metric_unresolved",
                    "无法确认统一的指标类型与缩放版本。",
                )
            if set(request.group_order) != set(current.group_order):
                raise InputValidationError(
                    "external_import_group_order_mismatch",
                    "确认的组别必须恰好覆盖检查到的全部组目录。",
                    detected_groups=list(current.group_order),
                    received_groups=list(request.group_order),
                )
            current_subjects = {item.subject_id for item in current.images}
            unknown_subjects = sorted(set(request.included_subject_ids) - current_subjects)
            if unknown_subjects:
                raise InputValidationError(
                    "external_import_subject_not_found",
                    "纳入名单包含检查目录中不存在的受试者。",
                    subject_ids=unknown_subjects,
                )
            included_groups = {
                item.group
                for item in current.images
                if item.subject_id in set(request.included_subject_ids)
            }
            if included_groups != set(request.group_order):
                raise InputValidationError(
                    "external_import_group_empty",
                    "每个确认组别至少需要一名纳入受试者。",
                )
            mask_evidence: dict[str, Any] = {}
            if request.mask_relative_path is not None:
                try:
                    mask_hash, mask_size, mask_grid = inspect_external_mask(
                        source, request.mask_relative_path
                    )
                except (OSError, ValueError) as exc:
                    raise InputValidationError(
                        "external_import_mask_invalid",
                        "选择的外部 mask 无法通过 NIfTI 检查。",
                        reason=f"{type(exc).__name__}: {exc}",
                    ) from exc
                if mask_grid != current.grid_signature:
                    raise InputValidationError(
                        "external_import_mask_grid_mismatch",
                        "外部 mask 必须与所有指标图拥有相同空间网格与仿射。",
                    )
                mask_evidence = {
                    "mask_relative_path": request.mask_relative_path,
                    "mask_sha256": mask_hash,
                    "mask_size_bytes": mask_size,
                    "mask_grid_signature": mask_grid,
                }
            excluded = tuple(sorted(current_subjects - set(request.included_subject_ids)))
            inclusion = ExternalMetricInclusion(
                included_subject_ids=tuple(request.included_subject_ids),
                excluded_subject_ids=excluded,
                group_order=tuple(request.group_order),
                coordinate_space=request.coordinate_space,
                coordinate_space_source=(
                    "unknown" if request.coordinate_space == "unknown" else "user_confirmed"
                ),
                actor=request.actor,
                reason=request.reason,
                unknown_processing_acknowledged=True,
                **mask_evidence,
            )
            record = inclusion.model_dump(mode="json")
            inclusion_hash = content_hash(
                {
                    "import_id": import_id,
                    "source_tree_hash": current.source_tree_hash,
                    "inclusion": record,
                }
            )
            record["inclusion_hash"] = inclusion_hash
            return record

        def finalize(inclusion_record: dict[str, Any]) -> ExternalAnalysisImportView:
            inclusion_hash = str(inclusion_record.pop("inclusion_hash"))
            result = self.repository.include_external_analysis_import(
                import_id,
                expected_version=request.expected_version,
                inclusion=inclusion_record,
                inclusion_hash=inclusion_hash,
            )
            assert result.inclusion is not None
            self.repository.append_event(
                project_id=result.project_id,
                run_id=None,
                event_type="ExternalAnalysisImportIncluded",
                severity="info",
                payload={
                    "import_id": result.import_id,
                    "source_tree_hash": result.inspection.source_tree_hash,
                    "inclusion_hash": inclusion_hash,
                    "included_subject_count": len(result.inclusion.included_subject_ids),
                    "excluded_subject_count": len(result.inclusion.excluded_subject_ids),
                    "group_count": len(result.inclusion.group_order),
                    "coordinate_space": result.inclusion.coordinate_space,
                    "actor": result.inclusion.actor,
                },
            )
            return result

        return self._idempotent_prepared(
            scope=f"external-analysis-imports:{import_id}:include",
            key=idempotency_key,
            request=request,
            response_type=ExternalAnalysisImportView,
            prepare=prepare,
            finalize=finalize,
        )
