"""Local run, QC, artifact, and analysis-helper use cases."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from neuroagent.analysis.cluster_roi import extract_cluster_means
from neuroagent.analysis.clusters import localize_clusters
from neuroagent.analysis.diagnostics import diagnose_dpabi_log
from neuroagent.analysis.dpabi_atlas import (
    get_atlas,
    list_dpabi_atlases,
    localize_cluster_map,
    peak_axial_slice,
)
from neuroagent.analysis.organization import build_dpabi_preview
from neuroagent.analysis.rag import answer_rsfmri_question, search_evidence
from neuroagent.analysis.roi import build_roi_tables, validate_roi_request
from neuroagent.analysis.tables import inspect_table
from neuroagent.analysis.templates import generate_ml_template
from neuroagent.application.artifact_content import (
    artifact_content_path,
    nifti_preview_png,
    nifti_preview_slice_count,
)
from neuroagent.application.contracts.agent import (
    ClusterLocalizationRequest,
    ClusterLocalizationView,
    MlTableInspectRequest,
    MlTableInspectView,
    MlTemplateCreateRequest,
    MlTemplateView,
    RoiTableCreateRequest,
    RoiTableView,
    RsFmriAnswerView,
    RsFmriQuestionRequest,
    RunDiagnosisRequest,
    RunDiagnosisView,
)
from neuroagent.application.contracts.plans import PlanRevisionView
from neuroagent.application.contracts.projects import (
    OrganizationPreviewRequest,
    OrganizationPreviewView,
)
from neuroagent.application.contracts.runs import (
    ArtifactView,
    ExecutionBackend,
    HeadMotionQcCreate,
    QcReviewApprove,
    QcReviewCreate,
    QcReviewView,
    QcSkipCreate,
    QcSkipView,
    RunAction,
    RunCreate,
    RuntimeEventView,
    RunView,
)
from neuroagent.application.contracts.statistics import (
    StatisticalAtlasPeakLocalization,
    StatisticalAtlasRegionCoverage,
    StatisticalAtlasView,
    StatisticalClusterRoiRow,
    StatisticalClusterRoiView,
    StatisticalDesignView,
    StatisticalResultClusterSliceView,
    StatisticalResultDetailView,
    StatisticalResultLocalizationView,
    StatisticalResultSliceInfoView,
    StatisticalResultView,
    StatisticsRunCreate,
)
from neuroagent.application.errors import ApplicationError, ConflictError, InputValidationError
from neuroagent.application.hashing import content_hash
from neuroagent.application.reporting import build_statistical_reproducibility_report
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
from neuroagent.application.statistical_result_facts import (
    build_method_summary,
    verified_result_design,
)
from neuroagent.application.work_tasks import require_statistical_work_plan
from neuroagent.domain.fmri.qc import QcReviewScope
from neuroagent.domain.fmri.results import (
    RegisteredArtifactMetadata,
    StatisticalArtifactRole,
    StatisticalAtlasSelection,
    StatisticalInferenceStatus,
    StatisticalResultManifest,
    StatisticalResultMode,
)
from neuroagent.domain.fmri.statistics import (
    CorrectionSpec,
    StatisticalDesignRevision,
    StatisticalQcSource,
)
from neuroagent.domain.lifecycle import (
    PlanState,
    WorkflowState,
)
from neuroagent.observability.work_timings import measure_work_stage


class RunMixin(BaseServiceMixin):
    repository: RunRepositoryPort
    artifact_repository: ArtifactCatalogRepositoryPort
    qc_repository: QcRepositoryPort
    conversation_repository: ConversationRepositoryPort
    external_import_repository: ExternalAnalysisRepositoryPort
    plan_repository: PlanRepositoryPort
    # The assembled ApplicationServices provides these helpers through
    # StatisticsMixin; declared here so RunMixin alone type-checks.
    _require_plan_current: Callable[..., None]
    _statistics_view: Callable[..., StatisticalDesignView]
    _require_skill_parameter_schema: Callable[..., None]

    def create_run(self, request: RunCreate, idempotency_key: str) -> RunView:
        self._validate_execution_backend(
            request.execution_backend, request.real_execution_confirmed
        )

        def prepare() -> None:
            plan = self.plan_repository.get_plan(request.plan_revision_id)
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
            plan = self.plan_repository.get_plan(request.plan_revision_id)
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
                "workspace_mode": request.workspace_mode.value,
                "executor_type": (
                    "matlab_preprocessing"
                    if request.execution_backend is ExecutionBackend.MATLAB
                    else "workflow_mock"
                ),
                "real_execution_confirmed": request.real_execution_confirmed,
                "qc_mode": request.qc_mode.value,
                "plan_hash": request.expected_plan_hash,
                "input_manifest_hash": plan.manifest_hash,
                "environment_hash": plan.environment_hash,
            }
            if plan.plan.get("skill_plan"):
                payload["workflow_plan"] = plan.plan["skill_plan"]
            if plan.state is PlanState.APPROVED:
                payload["approval_record_id"] = self.plan_repository.get_approved_plan_approval(
                    plan.plan_revision_id
                ).approval_id
            with measure_work_stage("queueing"):
                result = self.repository.create_run(
                    project_id=request.project_id,
                    plan_revision_id=request.plan_revision_id,
                    expected_plan_hash=request.expected_plan_hash,
                    max_attempts=request.max_attempts,
                    payload=payload,
                )
            self.event_store.append_event(
                project_id=result.project_id,
                run_id=result.run_id,
                event_type="RunQueued",
                severity="info",
                payload={
                    "plan_revision_id": result.plan_revision_id,
                    "execution_backend": request.execution_backend.value,
                    "workspace_mode": request.workspace_mode.value,
                    "executor_type": (
                        "matlab_preprocessing"
                        if request.execution_backend is ExecutionBackend.MATLAB
                        else "workflow_mock"
                    ),
                    "real_execution_confirmed": request.real_execution_confirmed,
                    "qc_mode": request.qc_mode.value,
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

    def skip_qc(self, run_id: str, request: QcSkipCreate, idempotency_key: str) -> QcSkipView:
        def action() -> QcSkipView:
            skip, run = self.qc_repository.skip_qc(run_id, request)
            self.event_store.append_event(
                project_id=run.project_id,
                run_id=run.run_id,
                event_type="QcSkipRecorded",
                severity="warning",
                payload={
                    "qc_skip_id": skip.qc_skip_id,
                    "decision_hash": skip.decision_hash,
                    "actor": skip.actor,
                    "reason": skip.reason,
                    "qc_review_revision_id": skip.qc_review_revision_id,
                },
            )
            return skip

        return self._idempotent(
            scope=f"runs:{run_id}:qc:skip",
            key=idempotency_key,
            request=request,
            response_type=QcSkipView,
            action=action,
        )

    def diagnose_run(self, run_id: str, request: RunDiagnosisRequest) -> RunDiagnosisView:
        """Classify a bounded log excerpt without invoking a model or executor."""

        run = self.repository.get_run(run_id)
        diagnosis = diagnose_dpabi_log(request.log_text)
        return RunDiagnosisView(run_id=run.run_id, diagnosis=diagnosis)

    def diagnose_latest_run(self, run_id: str) -> dict[str, object]:
        """Classify bounded local logs for a failed run without exposing paths."""

        run = self.repository.get_run(run_id)
        attempt_root = self._latest_attempt_root(run).resolve()
        log_root = (attempt_root / "logs").resolve()
        try:
            log_root.relative_to(attempt_root)
        except ValueError:
            return diagnose_dpabi_log("").model_dump(mode="json")
        allowed_names = {"matlab.log", "matlab.stdout.log", "matlab.stderr.log"}
        chunks: list[str] = []
        remaining = 64_000
        for path in sorted(log_root.rglob("*"), key=lambda item: str(item)):
            if not path.is_file() or path.name.lower() not in allowed_names:
                continue
            resolved = path.resolve()
            try:
                resolved.relative_to(attempt_root)
            except ValueError:
                continue
            try:
                with resolved.open("rb") as stream:
                    stream.seek(max(0, resolved.stat().st_size - min(remaining, 24_000)))
                    content = stream.read(min(remaining, 24_000)).decode("utf-8", errors="replace")
            except OSError:
                continue
            if content.strip():
                chunks.append(content)
                remaining -= len(content.encode("utf-8", errors="replace"))
            if remaining <= 0:
                break
        diagnosis = diagnose_dpabi_log("\n".join(chunks) if chunks else "")
        return diagnosis.model_dump(mode="json")

    def inspect_ml_table(self, request: MlTableInspectRequest) -> MlTableInspectView:
        project = self.project_repository.get_project(request.project_id)
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
        project = self.project_repository.get_project(request.project_id)
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

        def require_unstarted_work_task(plan: PlanRevisionView) -> None:
            task = require_statistical_work_plan(
                self.conversation_repository.get_conversation, plan
            )
            if task is not None and task.run_id is not None:
                existing = self.repository.get_run(task.run_id)
                if existing.plan_revision_id == plan.plan_revision_id:
                    raise ConflictError(
                        "statistics_work_run_already_created",
                        "此统计方案已有运行。请查看已有任务，重试请使用该运行的恢复入口。",  # noqa: RUF001
                        run_id=existing.run_id,
                    )

        def prepare() -> None:
            plan = self.plan_repository.get_plan(request.statistical_design_revision_id)
            require_unstarted_work_task(plan)
            design_view = self._statistics_view(plan)
            capability = next(
                (
                    item
                    for item in STATISTICAL_CAPABILITIES
                    if item["test"] == design_view.design.test.value
                ),
                None,
            )
            if capability is None or not capability["executable"]:
                raise ConflictError(
                    "statistics_capability_not_validated",
                    "该统计适配尚未通过本机 DPABI 验证，当前不可执行。",  # noqa: RUF001
                    test=design_view.design.test.value,
                    validation_status=(
                        capability["validation_status"] if capability is not None else "unknown"
                    ),
                )
            self._validate_statistics_qc_binding(design_view.design)
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
            plan = self.plan_repository.get_plan(request.statistical_design_revision_id)
            require_unstarted_work_task(plan)
            design_view = self._statistics_view(plan)
            self._validate_statistics_qc_binding(design_view.design)
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
                payload["approval_record_id"] = self.plan_repository.get_approved_plan_approval(
                    plan.plan_revision_id
                ).approval_id
            result = self.repository.create_run(
                project_id=request.project_id,
                plan_revision_id=request.statistical_design_revision_id,
                expected_plan_hash=request.expected_plan_hash,
                max_attempts=request.max_attempts,
                payload=payload,
            )
            work_context = plan.plan.get("work_context")
            if work_context is not None:
                self.conversation_repository.bind_statistics_task(
                    conversation_id=work_context["conversation_id"],
                    project_id=result.project_id,
                    proposal_hash=work_context["proposal_hash"],
                    plan_revision_id=plan.plan_revision_id,
                    run_id=result.run_id,
                )
            self.event_store.append_event(
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

    def _validate_statistics_qc_binding(self, design: StatisticalDesignRevision) -> None:
        if design.qc_source is StatisticalQcSource.EXTERNAL_IMPORT:
            assert design.external_import_id is not None
            imported = self.external_import_repository.get_external_analysis_import(
                design.external_import_id
            )
            if (
                imported.status != "included"
                or imported.selection is None
                or imported.inclusion_hash != design.external_import_hash
            ):
                raise ConflictError(
                    "external_import_binding_mismatch", "外部数据纳入记录已失配或缺少 mask。"
                )
            return
        if design.qc_source is StatisticalQcSource.APPROVED_REVIEW:
            assert design.qc_review_revision_id is not None and design.qc_review_hash is not None
            approved_qc = self.qc_repository.get_approved_qc_review(design.qc_review_revision_id)
            if approved_qc.review.content_hash != design.qc_review_hash:
                raise ConflictError("qc_review_hash_mismatch", "统计运行引用的 QC review 已失配。")
            return
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
            raise ConflictError("qc_skip_binding_mismatch", "统计运行引用的 QC 跳过记录已失配。")

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
        qc_review_hash: str | None,
        environment_hash: str,
        plan_hash: str,
        actor: str,
    ) -> StatisticalResultView:
        """Validate frozen evidence and persist the deterministic reproducibility report.

        Registration is a local use case: the report builder fail-closes on hash
        mismatches and incomplete manifests before any row is written.
        """

        run = self.repository.get_run(run_id)
        plan = self.plan_repository.get_plan(run.plan_revision_id)
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
        return self.artifact_repository.create_statistical_result(
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
        return self.artifact_repository.list_statistical_results(
            project_id=project_id, run_id=run_id
        )

    def get_statistical_result(self, result_id: str) -> StatisticalResultDetailView:
        detail = self.artifact_repository.get_statistical_result(result_id)
        try:
            manifest, _, _ = verified_result_design(detail)
            evidence: dict[str, dict[str, object]] = {}
            issues: list[str] = []
            for role in (
                StatisticalArtifactRole.CORRECTION_EVIDENCE,
                StatisticalArtifactRole.SOFTWARE_VERSION_EVIDENCE,
            ):
                meta = next((item for item in manifest.artifacts if item.role is role), None)
                if meta is None or meta.placeholder:
                    continue
                try:
                    artifact = self.artifact_repository.get_artifact(meta.artifact_id)
                    if (
                        artifact.checksum != meta.checksum_sha256
                        or artifact.size_bytes != meta.size_bytes
                    ):
                        raise ValueError("statistical evidence metadata changed")
                    path = artifact_content_path(
                        artifact,
                        self.repository.get_run(detail.run_id),
                        self.project_repository.get_project(detail.project_id),
                    )
                    if path.stat().st_size > 256_000:
                        raise ValueError("statistical evidence exceeds summary size limit")
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("statistical evidence is not a JSON object")
                    evidence[role.value] = data
                except (ApplicationError, OSError, ValueError):
                    issues.append(f"{role.value}_unavailable")
            correction_status = (
                "verified"
                if "correction_evidence" in evidence
                else "unavailable"
                if "correction_evidence_unavailable" in issues
                else "not_registered"
            )
            summary = build_method_summary(
                detail,
                correction_evidence=evidence.get("correction_evidence"),
                software_evidence=evidence.get("software_version_evidence"),
                correction_evidence_status=correction_status,
                evidence_issues=tuple(issues),
            )
            return detail.model_copy(update={"method_summary": summary})
        except (KeyError, TypeError, ValueError):
            # Historical reports remain readable; unverified facts are withheld.
            return detail.model_copy(update={"method_summary": None})

    def _statistical_result_preview_source(
        self, result_id: str
    ) -> tuple[StatisticalResultManifest, RegisteredArtifactMetadata, Path]:
        detail = self.artifact_repository.get_statistical_result(result_id)
        manifest = StatisticalResultManifest.model_validate(detail.manifest)
        if manifest.mode is not StatisticalResultMode.REAL or manifest.non_scientific:
            raise ConflictError(
                "synthetic_result_preview_blocked",
                "非科学占位结果不支持统计图切片预览。",
            )
        cluster_sources = {item.source_map_artifact_id for item in manifest.clusters}
        if len(cluster_sources) > 1:
            raise ConflictError(
                "statistics_preview_source_ambiguous",
                "一个统计结果中的簇引用了多个来源图，无法联动切片。",  # noqa: RUF001
            )
        if cluster_sources:
            map_artifact_id = next(iter(cluster_sources))
        else:
            preferred_role = (
                StatisticalArtifactRole.CORRECTED_STATISTICAL_MAP
                if manifest.correction is not None
                else StatisticalArtifactRole.UNCORRECTED_STATISTICAL_MAP
            )
            map_meta = next(
                (item for item in manifest.artifacts if item.role is preferred_role), None
            )
            if map_meta is None:
                raise ConflictError(
                    "statistics_preview_map_missing",
                    "统计结果没有可预览的统计图。",
                )
            map_artifact_id = map_meta.artifact_id
        map_meta = next(
            (
                item
                for item in manifest.artifacts
                if item.artifact_id == map_artifact_id
                and item.role
                in {
                    StatisticalArtifactRole.UNCORRECTED_STATISTICAL_MAP,
                    StatisticalArtifactRole.CORRECTED_STATISTICAL_MAP,
                }
            ),
            None,
        )
        if map_meta is None:
            raise ConflictError(
                "statistics_preview_map_invalid",
                "统计簇未绑定已登记的统计图。",
            )
        artifact = self.artifact_repository.get_artifact(map_artifact_id)
        run = self.repository.get_run(detail.run_id)
        project = self.project_repository.get_project(detail.project_id)
        path = artifact_content_path(artifact, run, project)
        return manifest, map_meta, path

    def statistical_result_slice_info(self, result_id: str) -> StatisticalResultSliceInfoView:
        manifest, map_meta, map_path = self._statistical_result_preview_source(result_id)
        slice_count = nifti_preview_slice_count(map_path)
        if slice_count < 1:
            raise InputValidationError(
                "statistical_preview_invalid", "统计图没有可预览的轴向切片。"
            )
        cluster_slices = tuple(
            StatisticalResultClusterSliceView(
                cluster_id=cluster.cluster_id,
                slice_index=peak_axial_slice(
                    map_path=map_path,
                    coordinate_mm=cluster.peak_coordinate_mm,
                ),
            )
            for cluster in manifest.clusters
        )
        return StatisticalResultSliceInfoView(
            result_id=result_id,
            map_artifact_id=map_meta.artifact_id,
            slice_count=slice_count,
            initial_slice_index=slice_count // 2,
            cluster_slices=cluster_slices,
        )

    def statistical_cluster_roi(
        self, result_id: str, cluster_id: str | None
    ) -> StatisticalClusterRoiView:
        detail = self.artifact_repository.get_statistical_result(result_id)
        manifest = StatisticalResultManifest.model_validate(detail.manifest)
        if manifest.mode is not StatisticalResultMode.REAL or manifest.non_scientific:
            raise ConflictError("synthetic_result_roi_blocked", "非科学占位结果不能用于 ROI 提取。")
        if not manifest.clusters:
            return StatisticalClusterRoiView(
                result_id=result_id,
                result_bundle_hash=detail.bundle_hash,
                cluster_id=None,
                status="no_significant_voxels"
                if manifest.inference_status is StatisticalInferenceStatus.NO_SIGNIFICANT_VOXELS
                else "no_reportable_clusters",
                voxel_count=0,
                rows=(),
            )
        cluster = next((item for item in manifest.clusters if item.cluster_id == cluster_id), None)
        if cluster is None:
            raise InputValidationError(
                "statistical_cluster_missing", "请选择该结果中已登记的显著簇。"
            )
        report = json.loads(detail.report_json)
        design_payload = report["statistical_design"]
        if content_hash(design_payload) != report["frozen_bindings"]["design_hash"]:
            raise ConflictError("statistical_roi_design_changed", "结果报告的统计设计哈希不一致。")
        design = StatisticalDesignRevision.model_validate(design_payload)
        if design.revision_id != manifest.design_revision_id:
            raise ConflictError("statistical_roi_design_changed", "结果和统计设计引用不一致。")
        run = self.repository.get_run(detail.run_id)
        project = self.project_repository.get_project(detail.project_id)
        source_meta = next(
            (
                item
                for item in manifest.artifacts
                if item.artifact_id == cluster.source_map_artifact_id
            ),
            None,
        )
        if source_meta is None or source_meta.role not in {
            StatisticalArtifactRole.CORRECTED_STATISTICAL_MAP,
            StatisticalArtifactRole.UNCORRECTED_STATISTICAL_MAP,
        }:
            raise ConflictError("statistical_roi_map_unbound", "ROI 未引用已登记的统计图。")
        map_path = artifact_content_path(
            self.artifact_repository.get_artifact(source_meta.artifact_id), run, project
        )
        image_paths: list[Path] = []
        imported = (
            self.external_import_repository.get_external_analysis_import(design.external_import_id)
            if design.external_import_id
            else None
        )
        if imported is not None:
            if (
                imported.project_id != project.project_id
                or imported.inclusion_hash != design.external_import_hash
                or imported.selection is None
            ):
                raise ConflictError("statistical_roi_source_changed", "外部 ROI 输入凭据不一致。")
            images = {
                (item.subject_id, item.condition): item for item in imported.inspection.images
            }
            refs = {(item.subject_id, item.condition): item for item in imported.selection.images}
            root = self.path_policy.validate_read_path(
                imported.source_path, project_roots=project.source_roots, expect_directory=True
            )
        for image in design.images:
            if imported is not None:
                key = (image.subject_id, image.condition)
                if key not in refs or refs[key].artifact_id != image.artifact_id:
                    raise ConflictError(
                        "statistical_roi_source_changed", "外部 ROI 影像引用不一致。"
                    )
                evidence = images[key]
                path = self.path_policy.validate_read_path(
                    root / evidence.relative_path,
                    project_roots=project.source_roots,
                    expect_directory=False,
                )
                if not path.is_relative_to(root) or path.stat().st_size != evidence.size_bytes:
                    raise ConflictError(
                        "statistical_roi_input_changed", "ROI 输入大小或路径已变化。"
                    )
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest() != evidence.sha256:
                    raise ConflictError("statistical_roi_input_changed", "ROI 输入内容已变化。")
            else:
                artifact = self.artifact_repository.get_artifact(image.artifact_id)
                if artifact.project_id != project.project_id:
                    raise ConflictError("statistical_roi_cross_project", "ROI 指标不属于结果项目。")
                source_run = self.repository.get_run(artifact.run_id)
                path = artifact_content_path(artifact, source_run, project)
            image_paths.append(path)
        try:
            means = extract_cluster_means(
                map_path=map_path, cluster=cluster, image_paths=tuple(image_paths)
            )
        except (OSError, ValueError) as exc:
            raise InputValidationError(
                "statistical_roi_invalid", "ROI 来源网格、峰值或簇范围无法复核。", reason=str(exc)
            ) from exc
        return StatisticalClusterRoiView(
            result_id=result_id,
            result_bundle_hash=detail.bundle_hash,
            cluster_id=cluster.cluster_id,
            source_map_artifact_id=cluster.source_map_artifact_id,
            status="descriptive",
            voxel_count=cluster.extent_voxels,
            rows=tuple(
                StatisticalClusterRoiRow(
                    subject_id=image.subject_id,
                    group=image.group,
                    condition=image.condition,
                    mean_value=value,
                )
                for image, value in zip(design.images, means, strict=True)
            ),
        )

    def statistical_result_slice_png(self, result_id: str, slice_index: int) -> bytes:
        _manifest, _map_meta, map_path = self._statistical_result_preview_source(result_id)
        slice_count = nifti_preview_slice_count(map_path)
        if slice_index >= slice_count:
            raise InputValidationError(
                "artifact_preview_slice_invalid", "切片序号超出 NIfTI 图像范围。"
            )
        return nifti_preview_png(map_path, slice_index=slice_index)

    def list_statistical_atlases(self) -> tuple[StatisticalAtlasView, ...]:
        configuration = self.environment_provider.configuration()
        return tuple(
            StatisticalAtlasView(
                atlas_id=item.atlas_id,
                name=item.name,
                coordinate_space=item.space,
                available=item.available,
                voxel_size_mm=item.voxel_size_mm,
                label_count=item.label_count,
                content_hash=item.content_hash,
                reason=item.reason,
            )
            for item in list_dpabi_atlases(configuration.dpabi_dir)
        )

    def localize_statistical_result(
        self, result_id: str, atlas_id: str
    ) -> StatisticalResultLocalizationView:
        detail = self.artifact_repository.get_statistical_result(result_id)
        manifest = StatisticalResultManifest.model_validate(detail.manifest)
        if manifest.mode is not StatisticalResultMode.REAL or manifest.non_scientific:
            raise ConflictError(
                "synthetic_result_localization_blocked",
                "非科学演示结果不能进行研究用途的脑区定位。",
            )
        if not manifest.clusters:
            status = (
                "no_significant_voxels"
                if manifest.inference_status is StatisticalInferenceStatus.NO_SIGNIFICANT_VOXELS
                else "no_reportable_clusters"
            )
            return StatisticalResultLocalizationView(
                result_id=result_id,
                result_bundle_hash=detail.bundle_hash,
                status=status,
                significant_voxel_count=manifest.significant_voxel_count,
                clusters=(),
            )
        configuration = self.environment_provider.configuration()
        availability = next(
            (
                item
                for item in list_dpabi_atlases(configuration.dpabi_dir)
                if item.atlas_id == atlas_id
            ),
            None,
        )
        if availability is None:
            raise InputValidationError("atlas_not_supported", "未登记此 DPABI 图谱。")
        if not availability.available or availability.content_hash is None:
            raise InputValidationError(
                "atlas_unavailable",
                availability.reason or "本机 DPABI 图谱不可用。",
            )
        report = json.loads(getattr(detail, "report_json", "{}"))
        raw_selection = report.get("localization_plan")
        if raw_selection is not None:
            selection = StatisticalAtlasSelection.model_validate(raw_selection)
            if selection.atlas_id != atlas_id:
                raise ConflictError(
                    "statistics_atlas_selection_mismatch",
                    "请求图谱与正式方案选择不一致，请使用方案中冻结的图谱。",  # noqa: RUF001
                )
            if selection.atlas_content_hash != availability.content_hash:
                raise ConflictError(
                    "statistics_atlas_changed",
                    "图谱文件已变化，不能替代报告中冻结的图谱。",  # noqa: RUF001
                )
        clusters = manifest.clusters
        spaces = {cluster.coordinate_space for cluster in clusters}
        if spaces and spaces != {availability.space}:
            unknown = any(space.lower() in {"unknown", "unreported", ""} for space in spaces)
            raise ConflictError(
                "statistics_coordinate_space_mismatch",
                "统计结果空间尚未确认，已阻止脑区定位。"  # noqa: RUF001
                if unknown
                else "统计结果的已确认空间与 DPABI 图谱不同，已阻止脑区定位。",  # noqa: RUF001
                result_spaces=sorted(spaces),
                atlas_space=availability.space,
            )

        run = self.repository.get_run(detail.run_id)
        project = self.project_repository.get_project(detail.project_id)
        localized: list[StatisticalAtlasPeakLocalization] = []
        try:
            source_map_ids = {cluster.source_map_artifact_id for cluster in clusters}
            if len(source_map_ids) > 1:
                raise ValueError("一个结果清单内的簇引用了多个统计来源图。")
            if clusters:
                source_map_id = next(iter(source_map_ids))
                artifact_meta = next(
                    (item for item in manifest.artifacts if item.artifact_id == source_map_id),
                    None,
                )
                if artifact_meta is None or artifact_meta.role not in {
                    StatisticalArtifactRole.UNCORRECTED_STATISTICAL_MAP,
                    StatisticalArtifactRole.CORRECTED_STATISTICAL_MAP,
                }:
                    raise ValueError("簇峰值未引用已登记的统计来源图。")
                artifact = self.artifact_repository.get_artifact(source_map_id)
                map_path = artifact_content_path(artifact, run, project)
                mapped_clusters = localize_cluster_map(
                    dpabi_dir=configuration.dpabi_dir,
                    atlas_id=atlas_id,
                    map_path=map_path,
                    clusters=clusters,
                )
                by_cluster = {item.cluster_id: item for item in mapped_clusters}
                if len(by_cluster) != len(clusters):
                    raise ValueError("定位结果簇编号不唯一。")
                for cluster in clusters:
                    mapped = by_cluster[cluster.cluster_id]
                    sample = mapped.atlas_peak
                    coverage = tuple(
                        StatisticalAtlasRegionCoverage(
                            label_value=item.label_value,
                            label=item.label,
                            voxel_count=item.voxel_count,
                            proportion=item.proportion,
                        )
                        for item in mapped.coverage
                    )
                    localized.append(
                        StatisticalAtlasPeakLocalization(
                            cluster_id=cluster.cluster_id,
                            peak_coordinate_mm=cluster.peak_coordinate_mm,
                            label_value=sample.label_value,
                            label=sample.label,
                            atlas_voxel_ijk=sample.voxel_ijk,
                            atlas_voxel_center_mm=sample.voxel_center_mm,
                            distance_to_voxel_center_mm=sample.distance_mm,
                            coverage=coverage,
                        )
                    )
            atlas, atlas_hash_after = get_atlas(configuration.dpabi_dir, atlas_id)
            if atlas_hash_after != availability.content_hash:
                raise ConflictError(
                    "atlas_changed_during_localization",
                    "定位过程中 DPABI 图谱文件发生变化，请重新载入图谱后再试。",  # noqa: RUF001
                )
        except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
            raise InputValidationError(
                "statistical_atlas_localization_failed",
                "脑区定位未完成：统计图空间、仿射或簇峰值证据未通过核验。",  # noqa: RUF001
                reason=str(exc),
            ) from exc
        return StatisticalResultLocalizationView(
            result_id=result_id,
            result_bundle_hash=detail.bundle_hash,
            significant_voxel_count=manifest.significant_voxel_count,
            atlas_id=atlas.atlas_id,
            atlas_name=atlas.name,
            atlas_content_hash=atlas_hash_after,
            coordinate_space=atlas.space,
            clusters=tuple(localized),
        )

    def cancel_run(self, run_id: str, request: RunAction, idempotency_key: str) -> RunView:
        def action() -> RunView:
            before = self.repository.get_run(run_id)
            result = self.repository.request_cancel(run_id, request.expected_version)
            self.event_store.append_event(
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
            if request.review_scope is QcReviewScope.PRE_STATISTICS:
                self._require_skill_parameter_schema(
                    "review-rsfmri-qc",
                    {
                        "input_manifest_hash": self.plan_repository.get_plan(
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
            review = self.qc_repository.create_qc_review(request)
            self.event_store.append_event(
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

    def create_head_motion_qc_review(
        self, run_id: str, request: HeadMotionQcCreate, idempotency_key: str
    ) -> QcReviewView:
        def action() -> QcReviewView:
            self._ensure_head_motion_qc_report(run_id, request)
            review = self.qc_repository.create_head_motion_qc_review(run_id, request)
            self.event_store.append_event(
                project_id=review.project_id,
                run_id=review.run_id,
                event_type="QcReviewRevisionCreated",
                severity="info",
                payload={
                    "review_revision_id": review.review.review_revision_id,
                    "review_hash": review.review.content_hash,
                    "review_scope": review.review.review_scope.value,
                    "included_count": len(review.review.included_subject_ids),
                    "excluded_count": len(review.review.excluded_subject_ids),
                    "artifact_count": len(review.review.metric_artifact_ids),
                    "automatic": False,
                },
            )
            return review

        return self._idempotent(
            scope=f"runs:{run_id}:head-motion-qc:create",
            key=idempotency_key,
            request=request,
            response_type=QcReviewView,
            action=action,
        )

    def _ensure_head_motion_qc_report(self, run_id: str, request: HeadMotionQcCreate) -> None:
        if any(
            artifact.artifact_type == "qc.head_motion_report"
            for artifact in self.artifact_repository.list_artifacts(run_id)
        ):
            return
        run = self.repository.get_run(run_id)
        if run.version != request.expected_run_version:
            raise ConflictError(
                "run_version_mismatch",
                "运行版本与请求不一致, 请刷新后重试。",
                expected=run.version,
                received=request.expected_run_version,
            )
        if run.state is not WorkflowState.QC_REVIEW:
            return
        plan = self.plan_repository.get_plan(run.plan_revision_id)
        skill_plan = plan.plan.get("skill_plan")
        if not isinstance(skill_plan, dict):
            raise ConflictError("head_motion_qc_plan_missing", "运行计划缺少 SkillPlan。")
        resolved_parameters = skill_plan.get("resolved_parameters")
        if not isinstance(resolved_parameters, list | tuple):
            raise ConflictError(
                "head_motion_qc_plan_parameters_missing",
                "运行计划缺少预处理参数。",
            )
        parameters = dict(resolved_parameters)
        preprocessing = parameters.get("preprocessing")
        if not isinstance(preprocessing, dict):
            return
        realignment = preprocessing.get("realignment")
        if not isinstance(realignment, dict) or not realignment.get("enabled"):
            return
        manifest_id = plan.plan.get("dataset_manifest_id")
        if not isinstance(manifest_id, str) or not manifest_id:
            raise ConflictError(
                "head_motion_qc_manifest_missing",
                "运行计划缺少冻结 manifest 引用。",
            )
        manifest = self.project_repository.get_manifest(manifest_id)
        if manifest.content_hash != plan.manifest_hash:
            raise ConflictError(
                "head_motion_qc_manifest_mismatch",
                "冻结 manifest 与计划哈希不一致。",
            )
        attempt_root = self._latest_attempt_root(run)
        realign_root = self._find_realign_parameter_root(attempt_root, skill_plan)
        evidence_path = attempt_root / "output" / "preprocessing-evidence.json"
        try:
            expected_counts = self._expected_volume_counts(evidence_path)
            report = self.head_motion_qc_parser(
                realign_root,
                input_manifest_hash=plan.manifest_hash,
                subject_ids=tuple(subject.subject_id for subject in manifest.subjects),
                criteria=request.criteria,
                expected_volume_counts=expected_counts,
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise InputValidationError(
                "head_motion_qc_parse_failed",
                f"头动 QC 证据解析失败: {exc}",
            ) from exc
        report_payload = report.model_dump(mode="json")
        report_json = (
            json.dumps(report_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        )
        relative_path = f"attempts/{attempt_root.name}/output/qc/head-motion-qc.json"
        output_path = attempt_root / "output" / "qc" / "head-motion-qc.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report_json, encoding="utf-8")
        digest = hashlib.sha256(report_json.encode("utf-8")).hexdigest()
        self.artifact_repository.register_artifacts(
            run.project_id,
            run.run_id,
            (
                {
                    "artifact_type": "qc.head_motion_report",
                    "relative_path": relative_path,
                    "checksum": digest,
                    "size_bytes": len(report_json.encode("utf-8")),
                    "provenance": {
                        "executor": "head_motion_qc_service",
                        "plan_hash": plan.plan_hash,
                        "head_motion_qc": report_payload,
                    },
                },
            ),
        )
        self.event_store.append_event(
            project_id=run.project_id,
            run_id=run.run_id,
            event_type="HeadMotionQcReportRegistered",
            severity="info",
            payload={
                "artifact_type": "qc.head_motion_report",
                "relative_path": relative_path,
                "included_count": len(report.included_subject_ids),
                "excluded_count": len(report.excluded_subject_ids),
            },
        )

    def _latest_attempt_root(self, run: RunView) -> Path:
        project = self.project_repository.get_project(run.project_id)
        work_root = Path(project.work_root).resolve()
        attempts_root = (work_root / run.run_id / "attempts").resolve()
        try:
            attempts_root.relative_to(work_root)
        except ValueError as exc:
            raise ConflictError(
                "head_motion_qc_attempt_path_invalid",
                "运行 attempt 目录越过项目工作区边界。",
            ) from exc
        if not attempts_root.is_dir():
            raise ConflictError(
                "head_motion_qc_attempt_missing",
                "当前运行没有可用于补跑头动 QC 的 attempt 目录。",
            )
        attempts = tuple(path for path in attempts_root.iterdir() if path.is_dir())
        if not attempts:
            raise ConflictError(
                "head_motion_qc_attempt_missing",
                "当前运行没有可用于补跑头动 QC 的 attempt 目录。",
            )
        return max(attempts, key=lambda path: (path.stat().st_mtime_ns, path.name))

    def _find_realign_parameter_root(
        self, attempt_root: Path, skill_plan: dict[str, object]
    ) -> Path:
        candidates = [
            attempt_root / "staging" / "RealignParameter",
            attempt_root / "RealignParameter",
        ]
        dataset_ref = skill_plan.get("dataset_ref")
        if isinstance(dataset_ref, str) and dataset_ref:
            dataset = self.project_repository.get_dataset(dataset_ref)
            project = self.project_repository.get_project(dataset.project_id)
            dataset_root = self.path_policy.validate_read_path(
                dataset.source_path,
                project_roots=project.source_roots,
                expect_directory=True,
            )
            candidates.append(dataset_root / "RealignParameter")
        for candidate in candidates:
            if candidate.is_dir():
                return candidate
        raise ConflictError(
            "head_motion_qc_realign_parameter_missing",
            "未找到 DPABI RealignParameter 目录, 不能补跑头动 QC。",
        )

    @staticmethod
    def _expected_volume_counts(evidence_path: Path) -> dict[str, int]:
        if not evidence_path.is_file():
            return {}
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        rows = evidence.get("outputs") if isinstance(evidence, dict) else None
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            return {}
        counts: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, dict) or row.get("metric") != "timeseries":
                continue
            subject_id = row.get("subject_id")
            volume_count = row.get("volume_count")
            if isinstance(subject_id, str) and isinstance(volume_count, int):
                counts[subject_id] = volume_count
        return counts

    def get_qc_review(self, review_revision_id: str) -> QcReviewView:
        return self.qc_repository.get_qc_review(review_revision_id)

    def get_run_qc_review(self, run_id: str) -> QcReviewView | None:
        self.repository.get_run(run_id)
        return self.qc_repository.get_latest_qc_review_for_run(run_id)

    def approve_qc_review(
        self,
        review_revision_id: str,
        request: QcReviewApprove,
        idempotency_key: str,
    ) -> QcReviewView:
        def action() -> QcReviewView:
            review, run = self.qc_repository.approve_qc_review(review_revision_id, request)
            self.event_store.append_event(
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
            self.event_store.append_event(
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
        return self.artifact_repository.list_artifacts(run_id)

    def get_artifact(self, artifact_id: str) -> ArtifactView:
        return self.artifact_repository.get_artifact(artifact_id)

    def artifact_content_path(self, artifact_id: str) -> Path:
        from neuroagent.application.artifact_content import artifact_content_path

        artifact = self.artifact_repository.get_artifact(artifact_id)
        run = self.repository.get_run(artifact.run_id)
        project = self.project_repository.get_project(run.project_id)
        return artifact_content_path(artifact, run, project)

    def artifact_preview_png(self, artifact_id: str) -> bytes:
        from neuroagent.application.artifact_content import nifti_preview_png

        return nifti_preview_png(self.artifact_content_path(artifact_id))

    def list_run_events(self, run_id: str, after_event_id: int = 0) -> list[RuntimeEventView]:
        self.repository.get_run(run_id)
        return self.event_store.list_events(run_id, after_event_id)

    def list_project_events(
        self, project_id: str, after_event_id: int = 0
    ) -> list[RuntimeEventView]:
        self.project_repository.get_project(project_id)
        return self.event_store.list_events(after_event_id=after_event_id, project_id=project_id)
