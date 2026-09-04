"""Artifacts and runtime events."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from neuroagent.application.contracts import (
    ArtifactView,
    RuntimeEventView,
    StatisticalResultDetailView,
    StatisticalResultView,
)
from neuroagent.application.errors import ConflictError, InputValidationError, NotFoundError
from neuroagent.application.hashing import canonical_json
from neuroagent.domain.fmri.artifacts import ArtifactLineage
from neuroagent.infrastructure.persistence.models import (
    ArtifactRow,
    PlanRevisionRow,
    ProjectRow,
    RuntimeEventRow,
    StatisticalResultRow,
    WorkflowRunRow,
)
from neuroagent.infrastructure.persistence.repository_mixins._base import (
    RepositoryBaseMixin,
    _as_utc,
    _id,
    _load,
    _validate_artifact_relative_path,
)
from neuroagent.observability.events import redact_event_payload
from neuroagent.observability.tracing import current_trace_id


class ArtifactEventMixin(RepositoryBaseMixin):
    # -- artifacts and events ----------------------------------------------

    def register_artifacts(
        self, project_id: str, run_id: str, artifacts: tuple[dict[str, Any], ...]
    ) -> None:
        with self._write_session() as session:
            run = session.get(WorkflowRunRow, run_id)
            if run is None:
                raise NotFoundError("run", run_id)
            self._register_artifacts_in_session(
                session,
                project_id=project_id,
                run=run,
                artifacts=artifacts,
            )

    def _register_artifacts_in_session(
        self,
        session: Session,
        *,
        project_id: str,
        run: WorkflowRunRow,
        artifacts: tuple[dict[str, Any], ...],
    ) -> list[ArtifactRow]:
        """Validate every artifact before adding any of them to the transaction."""

        if run.project_id != project_id:
            raise ConflictError("cross_project_run", "运行不属于指定项目。")
        plan = session.get(PlanRevisionRow, run.plan_revision_id)
        if plan is None:
            raise NotFoundError("plan_revision", run.plan_revision_id)

        prepared: list[ArtifactRow] = []
        for artifact in artifacts:
            relative_path = _validate_artifact_relative_path(artifact["relative_path"])
            artifact_id = _id()
            provenance = dict(artifact.get("provenance", {}))
            lineage_data = provenance.get("lineage")
            if lineage_data is not None:
                if not isinstance(lineage_data, dict):
                    raise InputValidationError(
                        "artifact_lineage_invalid",
                        "Artifact lineage 必须是结构化对象。",
                    )
                bound_lineage = {**lineage_data, "artifact_id": artifact_id}
                try:
                    lineage = ArtifactLineage.model_validate(bound_lineage)
                except ValidationError as exc:
                    raise InputValidationError(
                        "artifact_lineage_invalid",
                        "Artifact lineage 未通过类型校验。",
                    ) from exc
                if lineage.subject_manifest_hash != plan.manifest_hash:
                    raise ConflictError(
                        "artifact_manifest_mismatch",
                        "Artifact lineage 必须绑定来源计划的 manifest。",
                    )
                provenance["lineage"] = lineage.model_dump(mode="json")
            prepared.append(
                ArtifactRow(
                    artifact_id=artifact_id,
                    project_id=project_id,
                    run_id=run.run_id,
                    artifact_type=str(artifact["artifact_type"]),
                    relative_path=relative_path,
                    checksum=str(artifact["checksum"]),
                    size_bytes=int(artifact["size_bytes"]),
                    provenance_json=canonical_json(provenance),
                )
            )
        session.add_all(prepared)
        return prepared

    def list_artifacts(self, run_id: str) -> list[ArtifactView]:
        with self.database.session_factory() as session:
            rows = session.scalars(
                select(ArtifactRow)
                .where(ArtifactRow.run_id == run_id)
                .order_by(ArtifactRow.created_at)
            ).all()
            return [self._artifact(row) for row in rows]

    def get_artifact(self, artifact_id: str) -> ArtifactView:
        with self.database.session_factory() as session:
            row = session.get(ArtifactRow, artifact_id)
            if row is None:
                raise NotFoundError("artifact", artifact_id)
            return self._artifact(row)

    def assert_artifacts_belong_to_run(self, artifact_ids: tuple[str, ...], run_id: str) -> None:
        with self.database.session_factory() as session:
            for artifact_id in artifact_ids:
                row = session.get(ArtifactRow, artifact_id)
                if row is None:
                    raise NotFoundError("artifact", artifact_id)
                if row.run_id != run_id:
                    raise ConflictError(
                        "artifact_run_mismatch",
                        "Artifact 不属于冻结的 QC 运行。",
                        artifact_id=artifact_id,
                    )

    def create_statistical_result(
        self,
        *,
        project_id: str,
        run_id: str,
        design_revision_id: str,
        mode: str,
        non_scientific: bool,
        non_scientific_reason: str | None,
        bundle_hash: str,
        manifest: dict[str, Any],
        report_markdown: str,
        report_json: str,
        actor: str,
    ) -> StatisticalResultView:
        """Persist one frozen statistical report; identical re-registration is idempotent."""

        now = datetime.now(UTC)
        result_id = str(manifest.get("result_id") or "")
        if not result_id:
            raise InputValidationError("statistical_result_id_missing", "结果清单缺少 result_id。")
        with self._write_session() as session:
            run = session.get(WorkflowRunRow, run_id)
            if run is None:
                raise NotFoundError("run", run_id)
            if run.project_id != project_id:
                raise ConflictError(
                    "cross_project_run",
                    "统计结果所属运行不属于指定项目。",
                    expected=run.project_id,
                    received=project_id,
                )
            existing = session.get(StatisticalResultRow, result_id)
            if existing is not None:
                if existing.bundle_hash != bundle_hash:
                    raise ConflictError(
                        "statistical_result_conflict",
                        "相同 result_id 已登记不同内容的统计结果。",
                        result_id=result_id,
                    )
                return self._statistical_result_view(existing)
            row = StatisticalResultRow(
                result_id=result_id,
                project_id=project_id,
                run_id=run_id,
                design_revision_id=design_revision_id,
                mode=mode,
                non_scientific=non_scientific,
                non_scientific_reason=non_scientific_reason,
                bundle_hash=bundle_hash,
                manifest_json=canonical_json(manifest),
                report_markdown=report_markdown,
                report_json=report_json,
                version=1,
                created_at=now,
            )
            session.add(row)
            session.flush()
            session.add(
                RuntimeEventRow(
                    trace_id=current_trace_id(),
                    project_id=project_id,
                    run_id=run_id,
                    event_type="StatisticalResultRegistered",
                    severity="info",
                    payload_json=canonical_json(
                        redact_event_payload(
                            {
                                "actor": actor,
                                "result_id": result_id,
                                "design_revision_id": design_revision_id,
                                "mode": mode,
                                "bundle_hash": bundle_hash,
                                "synthetic": non_scientific,
                            }
                        )
                    ),
                )
            )
            return self._statistical_result_view(row)

    def list_statistical_results(
        self, *, project_id: str, run_id: str | None = None
    ) -> list[StatisticalResultView]:
        with self.database.session_factory() as session:
            if session.get(ProjectRow, project_id) is None:
                raise NotFoundError("project", project_id)
            query = select(StatisticalResultRow).where(
                StatisticalResultRow.project_id == project_id
            )
            if run_id is not None:
                query = query.where(StatisticalResultRow.run_id == run_id)
            rows = session.scalars(
                query.order_by(
                    StatisticalResultRow.created_at.desc(), StatisticalResultRow.result_id
                )
            ).all()
            return [self._statistical_result_view(row) for row in rows]

    def get_statistical_result(self, result_id: str) -> StatisticalResultDetailView:
        with self.database.session_factory() as session:
            row = session.get(StatisticalResultRow, result_id)
            if row is None:
                raise NotFoundError("statistical_result", result_id)
            return self._statistical_result_detail(row)

    def append_event(
        self,
        *,
        project_id: str | None,
        run_id: str | None,
        event_type: str,
        severity: str,
        payload: dict[str, object],
    ) -> None:
        with self._write_session() as session:
            session.add(
                RuntimeEventRow(
                    trace_id=current_trace_id(),
                    project_id=project_id,
                    run_id=run_id,
                    event_type=event_type,
                    severity=severity,
                    payload_json=canonical_json(redact_event_payload(payload)),
                )
            )

    def list_events(
        self,
        run_id: str | None = None,
        after_event_id: int = 0,
        *,
        project_id: str | None = None,
    ) -> list[RuntimeEventView]:
        with self.database.session_factory() as session:
            query = select(RuntimeEventRow).where(RuntimeEventRow.event_id > after_event_id)
            if run_id is not None:
                query = query.where(RuntimeEventRow.run_id == run_id)
            if project_id is not None:
                query = query.where(RuntimeEventRow.project_id == project_id)
            rows = session.scalars(query.order_by(RuntimeEventRow.event_id)).all()
            return [
                RuntimeEventView(
                    event_id=row.event_id,
                    trace_id=row.trace_id,
                    project_id=row.project_id,
                    run_id=row.run_id,
                    event_type=row.event_type,
                    severity=row.severity,
                    payload=_load(row.payload_json, {}),
                    created_at=_as_utc(row.created_at),
                )
                for row in rows
            ]
