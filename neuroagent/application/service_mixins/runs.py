"""Local run, QC, artifact, and analysis-helper use cases."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from neuroagent.analysis.clusters import localize_clusters
from neuroagent.analysis.diagnostics import diagnose_dpabi_log
from neuroagent.analysis.organization import build_dpabi_preview
from neuroagent.analysis.rag import answer_rsfmri_question, search_evidence
from neuroagent.analysis.roi import build_roi_tables, validate_roi_request
from neuroagent.analysis.tables import inspect_table
from neuroagent.analysis.templates import generate_ml_template
from neuroagent.application.contracts import (
    ArtifactView,
    ClusterLocalizationRequest,
    ClusterLocalizationView,
    ExecutionBackend,
    MlTableInspectRequest,
    MlTableInspectView,
    MlTemplateCreateRequest,
    MlTemplateView,
    OrganizationPreviewRequest,
    OrganizationPreviewView,
    PlanState,
    QcReviewApprove,
    QcReviewCreate,
    QcReviewView,
    RoiTableCreateRequest,
    RoiTableView,
    RsFmriAnswerView,
    RsFmriQuestionRequest,
    RunAction,
    RunCreate,
    RunDiagnosisRequest,
    RunDiagnosisView,
    RuntimeEventView,
    RunView,
    StatisticalDesignView,
    StatisticalResultDetailView,
    StatisticalResultView,
    StatisticsRunCreate,
    WorkflowState,
)
from neuroagent.application.errors import ConflictError, InputValidationError
from neuroagent.application.hashing import content_hash
from neuroagent.application.reporting import build_statistical_reproducibility_report
from neuroagent.application.service_mixins._base import BaseServiceMixin
from neuroagent.domain.fmri.results import StatisticalResultManifest
from neuroagent.domain.fmri.statistics import (
    CorrectionSpec,
    StatisticalDesignRevision,
    StatisticalTest,
)


class RunMixin(BaseServiceMixin):
    # The assembled NeuroAgentService provides these helpers through
    # StatisticsMixin; declared here so RunMixin alone type-checks.
    _require_plan_current: Callable[..., None]
    _statistics_view: Callable[..., StatisticalDesignView]
    _require_skill_parameter_schema: Callable[..., None]

    def create_run(self, request: RunCreate, idempotency_key: str) -> RunView:
        self._validate_execution_backend(
            request.execution_backend, request.real_execution_confirmed
        )

        def prepare() -> None:
            plan = self.repository.get_plan(request.plan_revision_id)
            if plan.plan_hash != request.expected_plan_hash:
                raise ConflictError(
                    "run_plan_hash_mismatch",
                    "运行请求的计划哈希与当前不可变计划不一致。",
                    expected=plan.plan_hash,
                    received=request.expected_plan_hash,
                )
            self._require_plan_current(plan)
            return None

        def finalize(_prepared: None) -> RunView:
            # The full source scan above stays outside SQLite's writer
            # transaction. After BEGIN IMMEDIATE, re-read the database locks
            # and environment fingerprint so a metadata revision in the
            # preparation window cannot enqueue a stale plan. Raw files cannot
            # be locked by SQLite and must be verified again before staging.
            plan = self.repository.get_plan(request.plan_revision_id)
            if plan.plan_hash != request.expected_plan_hash:
                raise ConflictError(
                    "run_plan_hash_mismatch",
                    "运行请求的计划哈希与当前不可变计划不一致。",
                    expected=plan.plan_hash,
                    received=request.expected_plan_hash,
                )
            self._require_plan_current(plan, verify_source_files=False)
            payload: dict[str, object] = {
                "outcome": request.mock_outcome.value,
                "delay_ms": request.mock_delay_ms,
                "execution_backend": request.execution_backend.value,
                "executor_type": (
                    "matlab_preprocessing"
                    if request.execution_backend is ExecutionBackend.MATLAB
                    else "workflow_mock"
                ),
                "real_execution_confirmed": request.real_execution_confirmed,
                "plan_hash": request.expected_plan_hash,
                "input_manifest_hash": plan.manifest_hash,
                "environment_hash": plan.environment_hash,
            }
            if plan.plan.get("skill_plan"):
                payload["workflow_plan"] = plan.plan["skill_plan"]
            if plan.state is PlanState.APPROVED:
                payload["approval_record_id"] = self.repository.get_approved_plan_approval(
                    plan.plan_revision_id
                ).approval_id
            result = self.repository.create_run(
                project_id=request.project_id,
                plan_revision_id=request.plan_revision_id,
                expected_plan_hash=request.expected_plan_hash,
                max_attempts=request.max_attempts,
                payload=payload,
            )
            self.repository.append_event(
                project_id=result.project_id,
                run_id=result.run_id,
                event_type="RunQueued",
                severity="info",
                payload={
                    "plan_revision_id": result.plan_revision_id,
                    "execution_backend": request.execution_backend.value,
                    "executor_type": (
                        "matlab_preprocessing"
                        if request.execution_backend is ExecutionBackend.MATLAB
                        else "workflow_mock"
                    ),
                    "real_execution_confirmed": request.real_execution_confirmed,
                    "plan_hash": request.expected_plan_hash,
                    "environment_hash": plan.environment_hash,
                    "approval_record_id": payload.get("approval_record_id"),
                    "version": result.version,
                },
            )
            return result

        return self._idempotent_prepared(
            scope=f"projects:{request.project_id}:runs:create",
            key=idempotency_key,
            request=request,
            response_type=RunView,
            prepare=prepare,
            finalize=finalize,
        )

    def get_run(self, run_id: str) -> RunView:
        return self.repository.get_run(run_id)

    def diagnose_run(self, run_id: str, request: RunDiagnosisRequest) -> RunDiagnosisView:
        """Classify a bounded log excerpt without invoking a model or executor."""

        run = self.repository.get_run(run_id)
        diagnosis = diagnose_dpabi_log(request.log_text)
        return RunDiagnosisView(run_id=run.run_id, diagnosis=diagnosis)

    def inspect_ml_table(self, request: MlTableInspectRequest) -> MlTableInspectView:
        project = self.repository.get_project(request.project_id)
        source = self.path_policy.validate_read_path(
            request.source_path,
            project_roots=project.source_roots,
            expect_directory=False,
        )
        try:
            inspection = inspect_table(source, max_rows=request.max_rows)
        except (OSError, ValueError) as exc:
            raise InputValidationError(
                "ml_table_inspection_failed",
                "上传的表格无法检查, 请确认格式、编码和文件完整性。",
                reason=str(exc),
            ) from exc
        return MlTableInspectView(
            project_id=request.project_id,
            source_path_name=source.name,
            inspection=inspection,
        )

    def create_ml_template(self, request: MlTemplateCreateRequest) -> MlTemplateView:
        try:
            template = generate_ml_template(request.design, source_filename=request.source_filename)
        except (OSError, ValueError) as exc:
            raise InputValidationError(
                "ml_template_generation_failed",
                "机器学习模板生成失败, 请检查设计字段。",
                reason=str(exc),
            ) from exc
        return MlTemplateView(template=template)

    def validate_roi_table(self, request: RoiTableCreateRequest) -> RoiTableView:
        issues = validate_roi_request(request.design)
        if issues:
            return RoiTableView(valid=False, issues=issues)
        try:
            long_rows, wide_rows = build_roi_tables(request.records)
        except ValueError as exc:
            return RoiTableView(valid=False, issues=(str(exc),))
        return RoiTableView(valid=True, issues=(), long_rows=long_rows, wide_rows=wide_rows)

    def localize_clusters(self, request: ClusterLocalizationRequest) -> ClusterLocalizationView:
        results = localize_clusters(
            request.clusters,
            request.atlas_points,
            max_distance_mm=request.max_distance_mm,
        )
        return ClusterLocalizationView(results=results, atlas_supplied=bool(request.atlas_points))

    def answer_rsfmri_question(self, request: RsFmriQuestionRequest) -> RsFmriAnswerView:
        # Remote retrieval is intentionally disabled until an explicit provider
        # and outbound-context policy are configured. Local evidence is bounded
        # to project documentation and declarative skills.
        roots = (
            Path(__file__).resolve().parents[3] / "docs",
            Path(__file__).resolve().parents[3] / "skills",
            Path(__file__).resolve().parents[3] / "neuroagent",
        )
        evidence = search_evidence(roots, request.question)
        answer = answer_rsfmri_question(request.question, evidence)
        return RsFmriAnswerView(answer=answer, remote_search_used=False)

    def organization_preview(self, request: OrganizationPreviewRequest) -> OrganizationPreviewView:
        project = self.repository.get_project(request.project_id)
        source = self.path_policy.validate_read_path(
            request.source_path,
            project_roots=project.source_roots,
            expect_directory=True,
        )
        subjects = {
            subject_id: {
                "functional": tuple(item.functional),
                "anatomical": tuple(item.anatomical),
                "inventory": tuple(item.inventory),
            }
            for subject_id, item in request.subjects.items()
        }
        try:
            preview = build_dpabi_preview(
                source,
                target_stage=request.target_stage,
                subjects=subjects,
            )
        except (OSError, ValueError) as exc:
            raise InputValidationError(
                "organization_preview_failed",
                "数据整理预览失败, 请检查相对路径和 DPABI 目标目录。",
                reason=str(exc),
            ) from exc
        return OrganizationPreviewView(
            project_id=request.project_id,
            source_path_name=source.name,
            preview=preview,
        )

    def list_runs(
        self,
        *,
        project_id: str | None = None,
        state: WorkflowState | None = None,
    ) -> list[RunView]:
        return self.repository.list_runs(project_id=project_id, state=state)

    def create_statistics_run(self, request: StatisticsRunCreate, idempotency_key: str) -> RunView:
        self._validate_execution_backend(
            request.execution_backend, request.real_execution_confirmed
        )

        def prepare() -> None:
            plan = self.repository.get_plan(request.statistical_design_revision_id)
            design_view = self._statistics_view(plan)
            if design_view.design.test not in {
                StatisticalTest.ONE_SAMPLE_T,
                StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
                StatisticalTest.PAIRED_T,
            }:
                raise ConflictError(
                    "statistics_test_not_in_mvp",
                    "v0.1 真实统计仅支持单样本、两独立样本和配对 t 检验。",
                    test=design_view.design.test.value,
                )
            approved_qc = self.repository.get_approved_qc_review(
                design_view.design.qc_review_revision_id
            )
            if approved_qc.review.content_hash != design_view.design.qc_review_hash:
                raise ConflictError(
                    "qc_review_hash_mismatch",
                    "统计运行引用的 QC review 已失配。",
                )
            if plan.plan_hash != request.expected_plan_hash:
                raise ConflictError(
                    "run_plan_hash_mismatch",
                    "运行请求的计划哈希与当前不可变统计设计不一致。",
                    expected=plan.plan_hash,
                    received=request.expected_plan_hash,
                )
            self._require_plan_current(plan)
            return None

        def finalize(_prepared: None) -> RunView:
            # Repeat statistical/QC, database-lock, and environment checks
            # while BEGIN IMMEDIATE prevents a concurrent metadata revision
            # from slipping between validation and run creation. The recursive
            # source-file scan just completed outside this writer transaction.
            plan = self.repository.get_plan(request.statistical_design_revision_id)
            design_view = self._statistics_view(plan)
            approved_qc = self.repository.get_approved_qc_review(
                design_view.design.qc_review_revision_id
            )
            if approved_qc.review.content_hash != design_view.design.qc_review_hash:
                raise ConflictError(
                    "qc_review_hash_mismatch",
                    "统计运行引用的 QC review 已失配。",
                )
            if plan.plan_hash != request.expected_plan_hash:
                raise ConflictError(
                    "run_plan_hash_mismatch",
                    "运行请求的计划哈希与当前不可变统计设计不一致。",
                    expected=plan.plan_hash,
                    received=request.expected_plan_hash,
                )
            self._require_plan_current(plan, verify_source_files=False)
            payload: dict[str, object] = {
                "outcome": "succeed",
                "delay_ms": 0,
                "run_kind": (
                    "statistics_mock"
                    if request.execution_backend is ExecutionBackend.MOCK
                    else "statistics_matlab"
                ),
                "execution_backend": request.execution_backend.value,
                "executor_type": (
                    "matlab_statistics"
                    if request.execution_backend is ExecutionBackend.MATLAB
                    else "workflow_mock"
                ),
                "real_execution_confirmed": request.real_execution_confirmed,
                "plan_hash": request.expected_plan_hash,
                "input_manifest_hash": plan.manifest_hash,
                "environment_hash": plan.environment_hash,
                "statistical_design": design_view.design.model_dump(mode="json"),
                "correction": (
                    design_view.correction.model_dump(mode="json")
                    if design_view.correction is not None
                    else None
                ),
            }
            if plan.state is PlanState.APPROVED:
                payload["approval_record_id"] = self.repository.get_approved_plan_approval(
                    plan.plan_revision_id
                ).approval_id
            result = self.repository.create_run(
                project_id=request.project_id,
                plan_revision_id=request.statistical_design_revision_id,
                expected_plan_hash=request.expected_plan_hash,
                max_attempts=request.max_attempts,
                payload=payload,
            )
            self.repository.append_event(
                project_id=result.project_id,
                run_id=result.run_id,
                event_type="StatisticsRunQueued",
                severity="info",
                payload={
                    "statistical_design_revision_id": result.plan_revision_id,
                    "plan_hash": request.expected_plan_hash,
                    "execution_backend": request.execution_backend.value,
                    "executor_type": (
                        "matlab_statistics"
                        if request.execution_backend is ExecutionBackend.MATLAB
                        else "workflow_mock"
                    ),
                    "real_execution_confirmed": request.real_execution_confirmed,
                    "environment_hash": plan.environment_hash,
                    "approval_record_id": payload.get("approval_record_id"),
                },
            )
            return result

        return self._idempotent_prepared(
            scope=f"projects:{request.project_id}:statistics-runs:create",
            key=idempotency_key,
            request=request,
            response_type=RunView,
            prepare=prepare,
            finalize=finalize,
        )

    def _validate_execution_backend(
        self, backend: ExecutionBackend, real_execution_confirmed: bool
    ) -> None:
        if backend is ExecutionBackend.MOCK:
            return
        if not real_execution_confirmed:
            raise ConflictError(
                "real_execution_confirmation_required",
                "真实 MATLAB 运行必须逐次确认。",
            )
        if not self.settings.enable_real_execution:
            raise ConflictError(
                "real_execution_disabled",
                "RSFMRI_ENABLE_REAL_EXECUTION 未启用, 真实运行已阻断。",
            )
        probe = self.environment_provider.current().probe
        if not probe.ready:
            raise ConflictError(
                "real_execution_environment_not_ready",
                "MATLAB/SPM/DPABI 环境未就绪, 真实运行已阻断。",
                environment_hash=probe.environment_hash,
            )

    def register_statistical_result(
        self,
        *,
        run_id: str,
        manifest: StatisticalResultManifest,
        design: StatisticalDesignRevision,
        correction: CorrectionSpec | None,
        qc_review_hash: str,
        environment_hash: str,
        plan_hash: str,
        actor: str,
    ) -> StatisticalResultView:
        """Validate frozen evidence and persist the deterministic reproducibility report.

        Registration is a local use case: the report builder fail-closes on hash
        mismatches and incomplete manifests before any row is written.
        """

        run = self.repository.get_run(run_id)
        plan = self.repository.get_plan(run.plan_revision_id)
        if plan.plan_hash != plan_hash:
            raise ConflictError(
                "statistical_result_plan_hash_mismatch",
                "结果登记引用的方案哈希与运行所属已批准统计设计不一致。",
                expected=plan.plan_hash,
                received=plan_hash,
            )
        if plan.plan.get("kind") == "statistical_design":
            frozen_design = StatisticalDesignRevision.model_validate(plan.plan.get("design"))
            if content_hash(frozen_design.model_dump(mode="json")) != content_hash(
                design.model_dump(mode="json")
            ):
                raise ConflictError(
                    "statistical_result_design_mismatch",
                    "结果登记引用的统计设计与运行所属已批准计划不一致。",
                )
        if manifest.run_id != run_id:
            raise ConflictError(
                "statistical_result_run_mismatch",
                "结果清单中的 run_id 与登记目标运行不一致。",
                expected=manifest.run_id,
                received=run_id,
            )
        report = build_statistical_reproducibility_report(
            manifest=manifest,
            design=design,
            correction=correction,
            qc_review_hash=qc_review_hash,
            environment_hash=environment_hash,
            plan_hash=plan_hash,
        )
        return self.repository.create_statistical_result(
            project_id=run.project_id,
            run_id=run_id,
            design_revision_id=manifest.design_revision_id,
            mode=manifest.mode.value,
            non_scientific=manifest.non_scientific,
            non_scientific_reason=manifest.non_scientific_reason,
            bundle_hash=report.bundle_hash,
            manifest=manifest.model_dump(mode="json"),
            report_markdown=report.markdown,
            report_json=report.json_text,
            actor=actor,
        )

    def list_statistical_results(
        self, *, project_id: str, run_id: str | None = None
    ) -> list[StatisticalResultView]:
        return self.repository.list_statistical_results(project_id=project_id, run_id=run_id)

    def get_statistical_result(self, result_id: str) -> StatisticalResultDetailView:
        return self.repository.get_statistical_result(result_id)

    def cancel_run(self, run_id: str, request: RunAction, idempotency_key: str) -> RunView:
        def action() -> RunView:
            before = self.repository.get_run(run_id)
            result = self.repository.request_cancel(run_id, request.expected_version)
            self.repository.append_event(
                project_id=result.project_id,
                run_id=run_id,
                event_type="RunCancellationRequested",
                severity="warning",
                payload={
                    "from_state": before.state.value,
                    "to_state": result.state.value,
                    "reason": request.reason,
                    "version": result.version,
                },
            )
            return result

        return self._idempotent(
            scope=f"runs:{run_id}:cancel",
            key=idempotency_key,
            request=request,
            response_type=RunView,
            action=action,
        )

    def create_qc_review(self, request: QcReviewCreate, idempotency_key: str) -> QcReviewView:
        def action() -> QcReviewView:
            self._require_skill_parameter_schema(
                "review-rsfmri-qc",
                {
                    "input_manifest_hash": self.repository.get_plan(
                        self.repository.get_run(request.run_id).plan_revision_id
                    ).manifest_hash,
                    "subject_order": [
                        *request.included_subject_ids,
                        *request.excluded_subject_ids,
                    ],
                    "metric_artifact_ids": list(request.metric_artifact_ids),
                    "checks": [check.model_dump(mode="json") for check in request.checks],
                },
            )
            review = self.repository.create_qc_review(request)
            self.repository.append_event(
                project_id=review.project_id,
                run_id=review.run_id,
                event_type="QcReviewRevisionCreated",
                severity="info",
                payload={
                    "review_revision_id": review.review.review_revision_id,
                    "review_hash": review.review.content_hash,
                    "included_count": len(review.review.included_subject_ids),
                    "excluded_count": len(review.review.excluded_subject_ids),
                    "artifact_count": len(review.review.metric_artifact_ids),
                },
            )
            return review

        return self._idempotent(
            scope=f"runs:{request.run_id}:qc-reviews:create",
            key=idempotency_key,
            request=request,
            response_type=QcReviewView,
            action=action,
        )

    def get_qc_review(self, review_revision_id: str) -> QcReviewView:
        return self.repository.get_qc_review(review_revision_id)

    def approve_qc_review(
        self,
        review_revision_id: str,
        request: QcReviewApprove,
        idempotency_key: str,
    ) -> QcReviewView:
        def action() -> QcReviewView:
            review, run = self.repository.approve_qc_review(review_revision_id, request)
            self.repository.append_event(
                project_id=review.project_id,
                run_id=review.run_id,
                event_type="QcReviewApproved" if request.approved else "QcReviewRejected",
                severity="info" if request.approved else "warning",
                payload={
                    "review_revision_id": review_revision_id,
                    "review_hash": review.review.content_hash,
                    "actor": request.actor,
                    "reason": request.reason,
                    "run_state": run.state.value,
                },
            )
            return review

        return self._idempotent(
            scope=f"qc-reviews:{review_revision_id}:approve",
            key=idempotency_key,
            request=request,
            response_type=QcReviewView,
            action=action,
        )

    def retry_run(self, run_id: str, request: RunAction, idempotency_key: str) -> RunView:
        def action() -> RunView:
            before = self.repository.get_run(run_id)
            result = self.repository.retry_run(run_id, expected_version=request.expected_version)
            self.repository.append_event(
                project_id=result.project_id,
                run_id=run_id,
                event_type="RunRetryRequested",
                severity="warning",
                payload={
                    "from_state": before.state.value,
                    "to_state": result.state.value,
                    "reason": request.reason,
                    "version": result.version,
                },
            )
            return result

        return self._idempotent(
            scope=f"runs:{run_id}:retry",
            key=idempotency_key,
            request=request,
            response_type=RunView,
            action=action,
        )

    def list_artifacts(self, run_id: str) -> list[ArtifactView]:
        self.repository.get_run(run_id)
        return self.repository.list_artifacts(run_id)

    def get_artifact(self, artifact_id: str) -> ArtifactView:
        return self.repository.get_artifact(artifact_id)

    def list_run_events(self, run_id: str, after_event_id: int = 0) -> list[RuntimeEventView]:
        self.repository.get_run(run_id)
        return self.repository.list_events(run_id, after_event_id)

    def list_project_events(
        self, project_id: str, after_event_id: int = 0
    ) -> list[RuntimeEventView]:
        self.repository.get_project(project_id)
        return self.repository.list_events(after_event_id=after_event_id, project_id=project_id)
