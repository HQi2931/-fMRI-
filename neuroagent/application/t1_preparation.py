"""Approved T1 preparation runs and append-only artifact-bound visual reviews."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from pydantic import Field

from neuroagent.application.contracts import (
    ExecutionBackend,
    MockOutcome,
    PlanRevisionView,
    PlanState,
    RunView,
    StrictModel,
    WorkflowState,
)
from neuroagent.application.errors import ConflictError, InputValidationError
from neuroagent.application.hashing import content_hash
from neuroagent.application.ports import RepositoryPort
from neuroagent.application.service_mixins._base import BaseServiceMixin


class T1PreparationRunCreate(StrictModel):
    project_id: str
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_backend: ExecutionBackend = ExecutionBackend.MOCK
    real_execution_confirmed: bool = False
    max_attempts: int = Field(default=1, ge=1, le=5)
    mock_outcome: MockOutcome = MockOutcome.SUCCEED
    mock_delay_ms: int = Field(default=0, ge=0, le=10_000)


class T1PreparationReviewCreate(StrictModel):
    expected_run_version: int = Field(ge=1)
    evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved: bool
    orientation_confirmed: bool
    crop_confirmed: bool
    origin_confirmed: bool
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)


class T1PreparationReviewView(StrictModel):
    run_id: str
    manifest_hash: str
    subject_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    evidence_hash: str
    review_record_id: str | None = None
    approved: bool = False
    non_scientific: bool = False
    subject_artifacts: dict[str, dict[str, str]] = Field(default_factory=dict)


class T1PreparationMixin(BaseServiceMixin):
    _require_plan_current: Callable[..., None]
    _validate_execution_backend: Callable[..., None]
    t1_preparation_artifact_validator: Callable[[Path, str], None]

    def create_t1_preparation_run(
        self, plan_revision_id: str, request: T1PreparationRunCreate, idempotency_key: str
    ) -> RunView:
        self._validate_execution_backend(
            request.execution_backend, request.real_execution_confirmed
        )

        def check(*, verify_source_files: bool = True) -> PlanRevisionView:
            plan = self.repository.get_plan(plan_revision_id)
            if (
                plan.project_id != request.project_id
                or plan.plan_hash != request.expected_plan_hash
            ):
                raise ConflictError("t1_preparation_plan_mismatch", "T1 准备请求与冻结方案不一致。")
            if plan.state is not PlanState.APPROVED:
                raise ConflictError("approval_required", "T1 准备需要先批准不可变方案。")
            self._require_plan_current(plan, verify_source_files=verify_source_files)
            manifest_id = plan.plan.get("dataset_manifest_id")
            if not isinstance(manifest_id, str):
                raise InputValidationError("t1_manifest_required", "方案没有冻结影像清单。")
            manifest = self.repository.get_manifest(manifest_id)
            if not manifest.subjects or any(
                len(s.anatomical_files) != 1 or not s.functional_files for s in manifest.subjects
            ):
                raise InputValidationError(
                    "t1_pairing_ambiguous", "每个受试者必须明确绑定一份 T1。"
                )
            return plan

        def finalize(_plan: PlanRevisionView) -> RunView:
            plan = check(verify_source_files=False)
            approval = self.repository.get_approved_plan_approval(plan_revision_id)
            payload: dict[str, object] = {
                "operation": "t1_preparation",
                "execution_backend": request.execution_backend.value,
                "executor_type": "matlab_preprocessing"
                if request.execution_backend is ExecutionBackend.MATLAB
                else "workflow_mock",
                "workspace_mode": "isolated",
                "real_execution_confirmed": request.real_execution_confirmed,
                "qc_mode": "skip",
                "outcome": request.mock_outcome.value,
                "delay_ms": request.mock_delay_ms,
                "plan_hash": plan.plan_hash,
                "input_manifest_hash": plan.manifest_hash,
                "environment_hash": plan.environment_hash,
                "approval_record_id": approval.approval_id,
                "workflow_plan": plan.plan.get("skill_plan"),
            }
            run = self.repository.create_run(
                project_id=request.project_id,
                plan_revision_id=plan_revision_id,
                expected_plan_hash=plan.plan_hash,
                max_attempts=request.max_attempts,
                payload=payload,
            )
            self.repository.append_event(
                project_id=run.project_id,
                run_id=run.run_id,
                event_type="T1PreparationQueued",
                severity="info",
                payload={
                    "operation": "t1_preparation",
                    "plan_hash": plan.plan_hash,
                    "input_manifest_hash": plan.manifest_hash,
                    "execution_backend": request.execution_backend.value,
                },
            )
            return run

        return self._idempotent_prepared(
            scope=f"plans:{plan_revision_id}:t1-preparation",
            key=idempotency_key,
            request=request,
            response_type=RunView,
            prepare=check,
            finalize=finalize,
        )

    def _t1_preparation_evidence(self, run_id: str) -> T1PreparationReviewView:
        run = self.repository.get_run(run_id)
        if run.state not in (WorkflowState.SUCCEEDED, WorkflowState.QC_REVIEW):
            raise ConflictError("t1_preparation_incomplete", "T1 准备运行尚未完成。")
        plan = self.repository.get_plan(run.plan_revision_id)
        manifest = self.repository.get_manifest(str(plan.plan["dataset_manifest_id"]))
        if manifest.content_hash != plan.manifest_hash:
            raise ConflictError("t1_preparation_manifest_changed", "准备清单与冻结方案不一致。")
        subjects = tuple(s.subject_id for s in manifest.subjects)
        roles = {
            "preprocessing.t1_prepared",
            "preprocessing.t1_review_image",
            "preprocessing.metadata_evidence",
        }
        artifacts = [a for a in self.repository.list_artifacts(run_id) if a.artifact_type in roles]
        project = self.repository.get_project(run.project_id)
        root = (Path(project.work_root) / run_id).resolve()
        for artifact in artifacts:
            path = (root / artifact.relative_path).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ConflictError(
                    "t1_preparation_artifact_missing", "T1 准备产物缺失或路径越界。"
                )
            if (
                path.stat().st_size != artifact.size_bytes
                or _file_sha256(path) != artifact.checksum
            ):
                raise ConflictError(
                    "t1_preparation_artifact_changed", "T1 准备产物已变化, 请重新准备与审查。"
                )
            try:
                self.t1_preparation_artifact_validator(path, artifact.artifact_type)
            except (ValueError, OSError) as exc:
                raise ConflictError(
                    "t1_preparation_artifact_invalid", "T1 准备产物或审查证据无法通过完整性检查。"
                ) from exc
        prepared = [a for a in artifacts if a.artifact_type == "preprocessing.t1_prepared"]
        actual = [
            str(
                a.provenance.get(
                    "subject_id", a.provenance.get("lineage", {}).get("subject_id", "")
                )
            )
            for a in prepared
        ]
        if len(actual) != len(subjects) or set(actual) != set(subjects):
            raise ConflictError(
                "t1_preparation_cohort_mismatch", "T1 准备产物与冻结受试者集合不一致。"
            )
        if not all(any(a.artifact_type == role for a in artifacts) for role in roles):
            raise ConflictError(
                "t1_preparation_evidence_missing", "缺少 T1 检查图或影像头信息证据。"
            )
        images = [a for a in artifacts if a.artifact_type == "preprocessing.t1_review_image"]
        image_subjects = [str(a.provenance.get("subject_id", "")) for a in images]
        if len(image_subjects) != len(subjects) or set(image_subjects) != set(subjects):
            raise ConflictError("t1_review_images_incomplete", "每个受试者均须有对应 T1 检查图。")
        evidence_ids = [
            a.artifact_id for a in artifacts if a.artifact_type == "preprocessing.metadata_evidence"
        ]
        subject_artifacts = {
            subject: {
                "prepared_artifact_id": prepared[actual.index(subject)].artifact_id,
                "review_image_artifact_id": images[image_subjects.index(subject)].artifact_id,
                "header_evidence_artifact_id": evidence_ids[0],
            }
            for subject in subjects
        }
        evidence = {
            "run_id": run_id,
            "plan_hash": plan.plan_hash,
            "manifest_hash": plan.manifest_hash,
            "cohort_hash": content_hash(subjects),
            "artifacts": sorted(
                [
                    {"id": a.artifact_id, "checksum": a.checksum, "size": a.size_bytes}
                    for a in artifacts
                ],
                key=lambda a: str(a["id"]),
            ),
        }
        return T1PreparationReviewView(
            run_id=run_id,
            manifest_hash=plan.manifest_hash,
            subject_ids=subjects,
            artifact_ids=tuple(a.artifact_id for a in artifacts),
            evidence_hash=content_hash(evidence),
            non_scientific=any(bool(a.provenance.get("non_scientific")) for a in artifacts),
            subject_artifacts=subject_artifacts,
        )

    def t1_preparation_preview_path(self, run_id: str, artifact_id: str) -> tuple[Path, str]:
        view = self._t1_preparation_evidence(run_id)
        if artifact_id not in view.artifact_ids:
            raise ConflictError("t1_preview_artifact_mismatch", "检查证据不属于本次准备运行。")
        artifact = self.repository.get_artifact(artifact_id)
        media_types = {
            "preprocessing.t1_review_image": "image/png",
            "preprocessing.metadata_evidence": "application/json",
        }
        if artifact.artifact_type not in media_types:
            raise InputValidationError("t1_preview_role_forbidden", "只能预览检查图和影像头信息。")
        run = self.repository.get_run(run_id)
        project = self.repository.get_project(run.project_id)
        return (Path(project.work_root) / run_id / artifact.relative_path).resolve(), media_types[
            artifact.artifact_type
        ]

    def get_t1_preparation_review(self, run_id: str) -> T1PreparationReviewView:
        view = self._t1_preparation_evidence(run_id)
        reviews = [
            e
            for e in self.repository.list_events(run_id)
            if e.event_type == "T1PreparationReviewed"
        ]
        if reviews and reviews[-1].payload.get("evidence_hash") == view.evidence_hash:
            return view.model_copy(
                update={
                    "review_record_id": reviews[-1].payload.get("review_record_id"),
                    "approved": reviews[-1].payload.get("approved") is True,
                }
            )
        return view

    def review_t1_preparation(
        self, run_id: str, request: T1PreparationReviewCreate, idempotency_key: str
    ) -> T1PreparationReviewView:
        def prepare() -> T1PreparationReviewView:
            run = self.repository.get_run(run_id)
            if run.version != request.expected_run_version:
                raise ConflictError("run_version_conflict", "运行版本已变化, 请重新加载。")
            view = self._t1_preparation_evidence(run_id)
            if request.evidence_hash != view.evidence_hash:
                raise ConflictError(
                    "t1_preparation_evidence_changed", "审查证据已变化, 请重新检查。"
                )
            if request.approved and not all(
                (request.orientation_confirmed, request.crop_confirmed, request.origin_confirmed)
            ):
                raise InputValidationError(
                    "t1_visual_review_required", "须确认 T1 方向、裁剪和原点后才能批准。"
                )
            return view

        def finalize(_view: T1PreparationReviewView) -> T1PreparationReviewView:
            view = prepare()
            record = str(uuid4())
            run = self.repository.get_run(run_id)
            self.repository.append_event(
                project_id=run.project_id,
                run_id=run_id,
                event_type="T1PreparationReviewed",
                severity="info",
                payload={
                    "review_record_id": record,
                    "evidence_hash": view.evidence_hash,
                    "manifest_hash": view.manifest_hash,
                    "cohort_hash": content_hash(view.subject_ids),
                    "artifact_ids": list(view.artifact_ids),
                    "approved": request.approved,
                    "orientation_confirmed": request.orientation_confirmed,
                    "crop_confirmed": request.crop_confirmed,
                    "origin_confirmed": request.origin_confirmed,
                    "actor": request.actor,
                    "reason": request.reason,
                },
            )
            return view.model_copy(
                update={"review_record_id": record, "approved": request.approved}
            )

        return self._idempotent_prepared(
            scope=f"runs:{run_id}:t1-preparation-review",
            key=idempotency_key,
            request=request,
            response_type=T1PreparationReviewView,
            prepare=prepare,
            finalize=finalize,
        )

    def resolve_reviewed_t1_bindings(
        self,
        run_id: str,
        manifest_hash: str,
        subject_ids: tuple[str, ...],
        *,
        allow_non_scientific: bool = False,
    ) -> tuple[dict[str, object], ...]:
        view = self.get_t1_preparation_review(run_id)
        if (
            not view.approved
            or view.manifest_hash != manifest_hash
            or view.subject_ids != subject_ids
        ):
            raise ConflictError(
                "t1_preparation_review_required", "须先审查与当前冻结清单一致的 T1 准备产物。"
            )
        if view.non_scientific and not allow_non_scientific:
            raise ConflictError("t1_mock_evidence_forbidden", "Mock 准备产物不能用于真实预处理。")
        by_subject = {}
        for artifact in self.repository.list_artifacts(run_id):
            if artifact.artifact_type != "preprocessing.t1_prepared":
                continue
            subject = str(
                artifact.provenance.get(
                    "subject_id", artifact.provenance.get("lineage", {}).get("subject_id", "")
                )
            )
            by_subject[subject] = {
                "subject_id": subject,
                "artifact_id": artifact.artifact_id,
                "relative_path": artifact.relative_path,
                "sha256": artifact.checksum,
                "size_bytes": artifact.size_bytes,
                "source_manifest_hash": manifest_hash,
                "review_record_id": view.review_record_id,
                "preparation_run_id": run_id,
            }
        return tuple(by_subject[s] for s in subject_ids)


def resolve_reviewed_t1_bindings(
    repository: RepositoryPort,
    artifact_validator: Callable[[Path, str], None],
    run_id: str,
    manifest_hash: str,
    subject_ids: tuple[str, ...],
    *,
    allow_non_scientific: bool = False,
) -> tuple[dict[str, object], ...]:
    """Worker-side gate: re-read approval events and verify bytes before staging."""
    reviewer = T1PreparationMixin()
    reviewer.repository = repository
    reviewer.t1_preparation_artifact_validator = artifact_validator
    return reviewer.resolve_reviewed_t1_bindings(
        run_id, manifest_hash, subject_ids, allow_non_scientific=allow_non_scientific
    )


def _file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()
