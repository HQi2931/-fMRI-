"""Persistence for inspected and explicitly included external analysis inputs."""

# ruff: noqa: RUF001

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from neuroagent.application.contracts import (
    ExternalAnalysisImportSummaryView,
    ExternalAnalysisImportView,
    ExternalAnalysisSelectionImageView,
    ExternalAnalysisSelectionView,
    ExternalDemographicsRevisionView,
)
from neuroagent.application.errors import ConflictError, NotFoundError
from neuroagent.application.hashing import canonical_json, content_hash
from neuroagent.domain.fmri.external_import import (
    ExternalMetricInclusion,
    ExternalMetricInspection,
    external_analysis_manifest_hash,
    external_mask_artifact_id,
    external_metric_artifact_id,
)
from neuroagent.infrastructure.persistence.models import (
    ExternalAnalysisImportRow,
    ExternalDemographicsRevisionRow,
    ProjectRow,
)
from neuroagent.infrastructure.persistence.repository_mixins._base import (
    RepositoryBaseMixin,
    _as_utc,
    _id,
    _load,
)


class ExternalAnalysisImportMixin(RepositoryBaseMixin):
    """Record immutable external scans and their one-way inclusion decision."""

    @staticmethod
    def _external_demographics(
        row: ExternalDemographicsRevisionRow,
    ) -> ExternalDemographicsRevisionView:
        content = _load(row.content_json, {})
        return ExternalDemographicsRevisionView(
            demographics_id=row.demographics_id,
            external_import_id=row.external_import_id,
            inclusion_hash=row.inclusion_hash,
            revision=row.revision,
            content_hash=row.content_hash,
            source_sha256=content["source_sha256"],
            row_count=len(content["rows"]),
            columns=content["columns"],
            created_at=_as_utc(row.created_at),
        )

    def create_external_demographics(
        self, import_id: str, *, expected_version: int, content: dict[str, Any]
    ) -> ExternalDemographicsRevisionView:
        with self._write_session() as session:
            imported = session.get(ExternalAnalysisImportRow, import_id)
            if imported is None:
                raise NotFoundError("external_analysis_import", import_id)
            self._check_version("external_analysis_import", imported.version, expected_version)
            if (
                imported.status != "included"
                or imported.inclusion_hash != content["inclusion_hash"]
            ):
                raise ConflictError("external_import_not_included", "外部纳入记录已变化。")
            revision = (
                session.scalar(
                    select(func.max(ExternalDemographicsRevisionRow.revision)).where(
                        ExternalDemographicsRevisionRow.external_import_id == import_id
                    )
                )
                or 0
            ) + 1
            row = ExternalDemographicsRevisionRow(
                demographics_id=_id(),
                external_import_id=import_id,
                inclusion_hash=imported.inclusion_hash,
                revision=revision,
                content_hash=content_hash(content),
                content_json=canonical_json(content),
            )
            session.add(row)
            imported.version += 1
            session.flush()
            return self._external_demographics(row)

    def get_latest_external_demographics(
        self, import_id: str
    ) -> ExternalDemographicsRevisionView | None:
        with self._read_session() as session:
            row = session.scalar(
                select(ExternalDemographicsRevisionRow)
                .where(ExternalDemographicsRevisionRow.external_import_id == import_id)
                .order_by(ExternalDemographicsRevisionRow.revision.desc())
                .limit(1)
            )
            return self._external_demographics(row) if row else None

    def get_external_demographics_content(
        self, demographics_id: str
    ) -> tuple[ExternalDemographicsRevisionView, dict[str, Any]]:
        with self._read_session() as session:
            row = session.get(ExternalDemographicsRevisionRow, demographics_id)
            if row is None:
                raise NotFoundError("external_demographics", demographics_id)
            return self._external_demographics(row), _load(row.content_json, {})

    @staticmethod
    def _external_analysis_import(row: ExternalAnalysisImportRow) -> ExternalAnalysisImportView:
        inspection = ExternalMetricInspection.model_validate(_load(row.inspection_json, {}))
        inclusion = (
            ExternalMetricInclusion.model_validate(_load(row.inclusion_json, {}))
            if row.inclusion_json
            else None
        )
        selection = None
        if (
            row.status == "included"
            and inclusion is not None
            and row.inclusion_hash is not None
            and inclusion.mask_sha256 is not None
        ):
            by_subject = {(item.subject_id, item.condition): item for item in inspection.images}
            selection = ExternalAnalysisSelectionView(
                input_manifest_hash=external_analysis_manifest_hash(
                    row.import_id, row.source_tree_hash, row.inclusion_hash
                ),
                mask_artifact_id=external_mask_artifact_id(row.import_id, inclusion.mask_sha256),
                images=tuple(
                    ExternalAnalysisSelectionImageView(
                        subject_id=subject_id,
                        group=by_subject[(subject_id, condition)].group,
                        condition=condition,
                        artifact_id=external_metric_artifact_id(
                            row.import_id, subject_id, condition
                        ),
                    )
                    for condition in (
                        inclusion.group_order
                        if inspection.layout == "paired_conditions"
                        else (None,)
                    )
                    for subject_id in inclusion.included_subject_ids
                ),
                group_order=inclusion.group_order,
            )
        return ExternalAnalysisImportView(
            import_id=row.import_id,
            project_id=row.project_id,
            source_path=row.source_path,
            status=row.status,
            version=row.version,
            inspection=inspection,
            inclusion=inclusion,
            inclusion_hash=row.inclusion_hash,
            selection=selection,
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
        )

    @staticmethod
    def _external_analysis_import_summary(
        row: ExternalAnalysisImportRow,
    ) -> ExternalAnalysisImportSummaryView:
        inspection = _load(row.inspection_json, {})
        images = inspection.get("images", [])
        group_counts: dict[str, int] = {
            str(group): 0 for group in inspection.get("group_order", [])
        }
        included_ids: set[str] | None = None
        inclusion: dict[str, Any] | None = None
        if row.inclusion_json:
            inclusion = _load(row.inclusion_json, {})
            included_ids = set(inclusion.get("included_subject_ids", []))
        for image in images:
            if not isinstance(image, dict):
                continue
            if included_ids is not None and image.get("subject_id") not in included_ids:
                continue
            group = str(image.get("group", ""))
            if group in group_counts:
                group_counts[group] += 1
        return ExternalAnalysisImportSummaryView(
            import_id=row.import_id,
            layout=inspection.get("layout", "grouped"),
            project_id=row.project_id,
            status=row.status,
            version=row.version,
            source_tree_hash=row.source_tree_hash,
            metric_kind=inspection.get("metric_kind"),
            scaling=inspection.get("scaling"),
            subject_count=len(
                {
                    image["subject_id"]
                    for image in images
                    if isinstance(image, dict)
                    and (included_ids is None or image.get("subject_id") in included_ids)
                }
            ),
            group_counts=group_counts,
            grid_signature=inspection.get("grid_signature"),
            coordinate_space=(
                inclusion.get("coordinate_space", "unknown")
                if inclusion is not None
                else inspection.get("coordinate_space", "unknown")
            ),
            blocking_issues=tuple(inspection.get("blocking_issues", [])),
            warnings=tuple(inspection.get("warnings", [])),
            unknown_processing_fields=tuple(inspection.get("unknown_processing_fields", [])),
            inclusion_hash=row.inclusion_hash,
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
        )

    def create_external_analysis_import(
        self,
        *,
        project_id: str,
        source_path: str,
        inspection: dict[str, Any],
    ) -> ExternalAnalysisImportView:
        with self._write_session() as session:
            if session.get(ProjectRow, project_id) is None:
                raise NotFoundError("project", project_id)
            now = datetime.now(UTC)
            row = ExternalAnalysisImportRow(
                import_id=_id(),
                project_id=project_id,
                source_path=source_path,
                status="needs_review",
                version=1,
                source_tree_hash=str(inspection["source_tree_hash"]),
                inspection_json=canonical_json(inspection),
                inclusion_json=None,
                inclusion_hash=None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return self._external_analysis_import(row)

    def get_external_analysis_import(self, import_id: str) -> ExternalAnalysisImportView:
        with self._read_session() as session:
            row = session.get(ExternalAnalysisImportRow, import_id)
            if row is None:
                raise NotFoundError("external_analysis_import", import_id)
            return self._external_analysis_import(row)

    def list_external_analysis_imports(
        self, *, project_id: str
    ) -> list[ExternalAnalysisImportSummaryView]:
        with self._read_session() as session:
            if session.get(ProjectRow, project_id) is None:
                raise NotFoundError("project", project_id)
            rows = session.scalars(
                select(ExternalAnalysisImportRow)
                .where(ExternalAnalysisImportRow.project_id == project_id)
                .order_by(
                    ExternalAnalysisImportRow.created_at.desc(),
                    ExternalAnalysisImportRow.import_id,
                )
            ).all()
            return [self._external_analysis_import_summary(row) for row in rows]

    def include_external_analysis_import(
        self,
        import_id: str,
        *,
        expected_version: int,
        inclusion: dict[str, Any],
        inclusion_hash: str,
    ) -> ExternalAnalysisImportView:
        with self._write_session() as session:
            row = session.get(ExternalAnalysisImportRow, import_id)
            if row is None:
                raise NotFoundError("external_analysis_import", import_id)
            if row.version != expected_version:
                raise ConflictError(
                    "external_import_version_mismatch",
                    "外部分析目录的登记版本已变化，请重新检查后再纳入。",
                    expected=expected_version,
                    actual=row.version,
                )
            if row.status != "needs_review":
                raise ConflictError(
                    "external_import_already_included",
                    "此外部分析目录已纳入；如需变更，应重新检查并创建新的导入记录。",
                    import_id=import_id,
                )
            row.status = "included"
            row.version += 1
            row.inclusion_json = canonical_json(inclusion)
            row.inclusion_hash = inclusion_hash
            row.updated_at = datetime.now(UTC)
            session.flush()
            return self._external_analysis_import(row)
