"""Deterministic actions exposed by the Work conversation mode."""

# ruff: noqa: RUF001

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from neuroagent.application.contracts import (
    ConversationAction,
    ConversationToolStatus,
    ConversationTurnCreate,
    DatasetCreate,
    DatasetView,
    ExecutionBackend,
    ExecutionWorkspaceMode,
    ManifestRevisionView,
    ManifestScanRequest,
    PlanRevisionView,
    PlanState,
    ProjectCreate,
    ProjectView,
    QcReviewView,
    RunCreate,
    RunView,
    SkillPlanResolveRequest,
    SkillPlanResolveView,
    StatisticalResultView,
)
from neuroagent.application.errors import ApplicationError, ConflictError, InputValidationError
from neuroagent.application.ports import PathPolicyPort, RepositoryPort


@dataclass(frozen=True, slots=True)
class WorkActionResult:
    assistant_content: str
    payload: dict[str, Any] = field(default_factory=dict)
    tool: dict[str, Any] | None = None
    workspace_path: str | None = None
    project_id: str | None = None
    active_run_id: str | None = None


class ConversationWorkCoordinator:
    """Translate a Work request into one safe application action."""

    def __init__(
        self,
        *,
        repository: RepositoryPort,
        path_policy: PathPolicyPort,
        create_project: Callable[[ProjectCreate, str], ProjectView],
        create_dataset: Callable[[str, DatasetCreate, str], DatasetView],
        inspect_dataset: Callable[[str, ManifestScanRequest, str], ManifestRevisionView],
        resolve_skill_plan: Callable[[SkillPlanResolveRequest, str], SkillPlanResolveView],
        validate_plan_current: Callable[[PlanRevisionView], None],
        get_qc_review: Callable[[str], QcReviewView],
        list_statistical_results: Callable[..., list[StatisticalResultView]],
        get_run: Callable[[str], RunView],
        create_run: Callable[[RunCreate, str], RunView],
        tool_result: Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]],
    ) -> None:
        self._repository = repository
        self._path_policy = path_policy
        self._create_project = create_project
        self._create_dataset = create_dataset
        self._inspect_dataset = inspect_dataset
        self._resolve_skill_plan = resolve_skill_plan
        self._validate_plan_current = validate_plan_current
        self._get_qc_review = get_qc_review
        self._list_statistical_results = list_statistical_results
        self._get_run = get_run
        self._create_run = create_run
        self._tool_result = tool_result

    def execute(
        self,
        request: ConversationTurnCreate,
        *,
        workspace_path: str | None,
        active_run_id: str | None,
        project_id: str | None,
        idempotency_key: str,
    ) -> WorkActionResult:
        action = self._resolve_action(request, workspace_path, active_run_id)
        try:
            if action is ConversationAction.SETUP_WORKSPACE:
                return self._setup_workspace(request, workspace_path, idempotency_key)
            if action is ConversationAction.PREPARE_PREPROCESSING_PLAN:
                return self._prepare_preprocessing_plan(request, project_id, idempotency_key)
            if action is ConversationAction.PREVIEW_PREPROCESSING_RUN:
                return self._preview_preprocessing_run(request, workspace_path, project_id)
            if action is ConversationAction.GET_PROGRESS:
                return self._progress(active_run_id)
            if action is ConversationAction.GET_QC_STATUS:
                return self._qc_status(request, active_run_id, project_id)
            if action is ConversationAction.GET_STATISTICAL_RESULTS:
                return self._statistical_results(request, active_run_id, project_id)
            if action is ConversationAction.START_PREPROCESSING:
                return self._start_preprocessing(
                    request,
                    workspace_path=workspace_path,
                    project_id=project_id,
                    idempotency_key=idempotency_key,
                )
            return WorkActionResult(
                "我已记录这条工作要求。你可以让我准备预处理方案、查看运行进度，"
                "或在已有审核计划后明确启动 DPABI 预处理。",
                workspace_path=workspace_path,
                project_id=project_id,
                active_run_id=active_run_id,
            )
        except ApplicationError as exc:
            return WorkActionResult(
                assistant_content=exc.message,
                tool={
                    "tool_name": self._tool_name(action),
                    "status": ConversationToolStatus.FAILED.value,
                    "input": {"workspace_path": workspace_path},
                    "output": {},
                    "error": exc.message,
                },
                workspace_path=workspace_path,
                project_id=project_id,
                active_run_id=active_run_id,
            )

    def _setup_workspace(
        self,
        request: ConversationTurnCreate,
        workspace_path: str | None,
        idempotency_key: str,
    ) -> WorkActionResult:
        if workspace_path is None:
            return WorkActionResult("请先点击“浏览”并选择工作区。")
        if request.workspace_setup is None:
            return WorkActionResult(
                assistant_content="建立项目还需要项目名称、数据集名称和独立工作目录。",
                tool={
                    "tool_name": "setup_workspace",
                    "status": ConversationToolStatus.AWAITING_CONFIRMATION.value,
                    "input": {"workspace_path": workspace_path},
                    "output": {},
                },
                workspace_path=workspace_path,
            )
        selected_workspace = self._path_policy.validate_project_source_root(workspace_path)
        setup = request.workspace_setup
        project = self._create_project(
            ProjectCreate(
                name=setup.project_name,
                source_roots=[str(selected_workspace)],
                work_root=setup.work_root,
            ),
            f"{idempotency_key}:project",
        )
        dataset = self._create_dataset(
            project.project_id,
            DatasetCreate(
                name=setup.dataset_name,
                source_path=str(selected_workspace),
                expected_project_version=project.version,
            ),
            f"{idempotency_key}:dataset",
        )
        manifest = self._inspect_dataset(
            dataset.dataset_id,
            ManifestScanRequest(expected_dataset_version=dataset.version, report_only=True),
            f"{idempotency_key}:manifest",
        )
        project = self._repository.get_project(project.project_id)
        dataset = self._repository.get_dataset(dataset.dataset_id)
        payload = {
            "project": project.model_dump(mode="json"),
            "dataset": dataset.model_dump(mode="json"),
            "manifest": manifest.model_dump(mode="json"),
        }
        return WorkActionResult(
            assistant_content=(
                f"工作区已登记为项目“{project.name}”，并冻结了当前文件清单。"
                "目录内容和 DPABI 输入结构由你维护。"
                "下一步请明确填写预处理和指标参数；系统不会补充科研默认值。"
            ),
            payload=payload,
            tool=self._tool_result(
                "setup_workspace",
                {
                    "workspace_path": str(selected_workspace),
                    "project_name": setup.project_name,
                    "dataset_name": setup.dataset_name,
                },
                payload,
            ),
            workspace_path=str(selected_workspace),
            project_id=project.project_id,
        )

    def _prepare_preprocessing_plan(
        self,
        request: ConversationTurnCreate,
        project_id: str | None,
        idempotency_key: str,
    ) -> WorkActionResult:
        if request.skill_plan_intent is None or request.expected_project_version is None:
            return WorkActionResult(
                assistant_content=(
                    "生成方案前必须提交完整的科研参数、参数来源证据和当前项目版本。"
                    "请在方案表单中逐项确认，系统不会根据对话猜测 TR、频段或预处理选择。"
                ),
                tool={
                    "tool_name": "prepare_preprocessing_plan",
                    "status": ConversationToolStatus.AWAITING_CONFIRMATION.value,
                    "input": {"project_id": project_id},
                    "output": {},
                },
                project_id=project_id,
            )
        intent = request.skill_plan_intent
        if project_id is not None and intent.project_id != project_id:
            raise ConflictError("conversation_project_mismatch", "方案不属于当前对话绑定的项目。")
        resolved = self._resolve_skill_plan(
            SkillPlanResolveRequest(
                request=intent,
                expected_project_version=request.expected_project_version,
                supersedes_plan_revision_id=request.plan_revision_id,
                validation_mode="report_only",
            ),
            f"{idempotency_key}:skill-plan",
        )
        manifest_profile: dict[str, Any] = {}
        dataset = self._repository.get_dataset(intent.dataset_ref)
        if dataset.current_manifest_id is not None:
            manifest = self._repository.get_manifest(dataset.current_manifest_id)
            manifest_content = self._repository.get_manifest_content(manifest.manifest_id)
            manifest_profile = manifest.profile.model_dump(mode="json")
            manifest_profile.update(
                {
                    "input_stage": manifest_content.get("input_stage"),
                    "output_directories": manifest_content.get("output_directories", []),
                }
            )
        payload = {
            "skill_plan": resolved.skill_plan.model_dump(mode="json"),
            "plan_revision": resolved.plan_revision.model_dump(mode="json"),
            "manifest_profile": manifest_profile,
        }
        return WorkActionResult(
            assistant_content=(
                "不可变预处理方案已编译并通过阻断校验。"
                f"计划版本为 {resolved.plan_revision.revision}，状态为 "
                f"{resolved.plan_revision.state.value}；请审查 DAG、警告和哈希后再批准。"
            ),
            payload=payload,
            tool=self._tool_result(
                "prepare_preprocessing_plan",
                {
                    "project_id": intent.project_id,
                    "dataset_id": intent.dataset_ref,
                    "manifest_hash": intent.input_manifest_hash,
                },
                payload,
            ),
            project_id=intent.project_id,
        )

    def _progress(self, active_run_id: str | None) -> WorkActionResult:
        if active_run_id is None:
            return WorkActionResult("当前对话尚未启动运行。")
        run = self._get_run(active_run_id)
        payload = {"run": run.model_dump(mode="json")}
        return WorkActionResult(
            assistant_content=(
                f"运行 {run.run_id[:8]} 当前状态为 {run.state.value}，已执行 {run.attempt} 次。"
            ),
            payload=payload,
            tool=self._tool_result("get_run_progress", {"run_id": active_run_id}, payload),
            active_run_id=active_run_id,
        )

    def _qc_status(
        self,
        request: ConversationTurnCreate,
        active_run_id: str | None,
        project_id: str | None,
    ) -> WorkActionResult:
        if request.qc_review_id is None:
            return WorkActionResult(
                assistant_content=(
                    "当前请求没有指定 QC review；请先在质量控制页创建或选择一个版本。"
                ),
                tool={
                    "tool_name": "get_qc_status",
                    "status": ConversationToolStatus.AWAITING_CONFIRMATION.value,
                    "input": {"run_id": request.target_run_id or active_run_id},
                    "output": {},
                },
                project_id=project_id,
                active_run_id=active_run_id,
            )
        review = self._get_qc_review(request.qc_review_id)
        if project_id is not None and review.project_id != project_id:
            raise ConflictError("cross_project_qc_review", "QC review 不属于当前对话项目。")
        target_run_id = request.target_run_id or active_run_id
        if target_run_id is not None and review.run_id != target_run_id:
            raise ConflictError("conversation_run_qc_mismatch", "QC review 不属于当前运行。")
        payload = {"qc_review": review.model_dump(mode="json")}
        return WorkActionResult(
            assistant_content=(
                f"QC review {review.review.review_revision_id[:8]} 当前状态为 "
                f"{review.state.value}；纳入 {len(review.review.included_subject_ids)} 名，"
                f"排除 {len(review.review.excluded_subject_ids)} 名受试者。"
            ),
            payload=payload,
            tool=self._tool_result(
                "get_qc_status",
                {"qc_review_id": request.qc_review_id, "run_id": review.run_id},
                payload,
            ),
            project_id=review.project_id,
            active_run_id=review.run_id,
        )

    def _statistical_results(
        self,
        request: ConversationTurnCreate,
        active_run_id: str | None,
        project_id: str | None,
    ) -> WorkActionResult:
        if project_id is None:
            return WorkActionResult(
                assistant_content="请先选择或建立项目，再查询统计结果。",
                tool={
                    "tool_name": "get_statistical_results",
                    "status": ConversationToolStatus.AWAITING_CONFIRMATION.value,
                    "input": {},
                    "output": {},
                },
            )
        run_id = request.target_run_id
        results = self._list_statistical_results(project_id=project_id, run_id=run_id)
        payload = {"statistical_results": [item.model_dump(mode="json") for item in results]}
        if results:
            scientific_count = sum(not item.non_scientific for item in results)
            message = (
                f"找到 {len(results)} 个已登记统计结果，"
                f"其中 {scientific_count} 个具有真实科学证据合同。"
            )
        else:
            message = "当前项目尚无符合条件的已登记统计结果。"
        return WorkActionResult(
            assistant_content=message,
            payload=payload,
            tool=self._tool_result(
                "get_statistical_results",
                {"project_id": project_id, "run_id": run_id},
                payload,
            ),
            project_id=project_id,
            active_run_id=active_run_id,
        )

    def _preview_preprocessing_run(
        self,
        request: ConversationTurnCreate,
        workspace_path: str | None,
        project_id: str | None,
    ) -> WorkActionResult:
        missing = [
            name
            for name, value in (
                ("project_id", project_id),
                ("plan_revision_id", request.plan_revision_id),
                ("expected_plan_hash", request.expected_plan_hash),
            )
            if value is None
        ]
        if missing:
            return WorkActionResult(
                assistant_content="运行预览需要当前项目、已验证计划和精确计划哈希。",
                tool={
                    "tool_name": "preview_dpabi_preprocessing",
                    "status": ConversationToolStatus.AWAITING_CONFIRMATION.value,
                    "input": {"missing": missing},
                    "output": {},
                },
                workspace_path=workspace_path,
                project_id=project_id,
            )
        assert project_id is not None
        assert request.plan_revision_id is not None
        assert request.expected_plan_hash is not None
        project = self._repository.get_project(project_id)
        plan = self._repository.get_plan(request.plan_revision_id)
        if plan.project_id != project_id:
            raise ConflictError("cross_project_plan", "审批计划不属于当前项目。")
        if plan.plan_hash != request.expected_plan_hash:
            raise ConflictError("run_plan_hash_mismatch", "运行预览的计划哈希已失配。")
        if plan.state is not PlanState.APPROVED:
            raise ConflictError("approved_plan_required", "只有已批准计划可以进入运行预览。")
        dataset_ref = plan.plan.get("skill_plan", {}).get("dataset_ref")
        if not isinstance(dataset_ref, str):
            raise InputValidationError(
                "preprocessing_skill_plan_required",
                "原位 DPABI 运行只接受已编译的预处理 SkillPlan。",
            )
        dataset = self._repository.get_dataset(dataset_ref)
        bound_workspace = self._path_policy.validate_read_path(
            dataset.source_path,
            project_roots=project.source_roots,
            expect_directory=True,
        )
        self._validate_workspace_match(workspace_path, bound_workspace, project.source_roots)
        self._validate_plan_current(plan)
        total_space_bytes, free_space_bytes = self._path_policy.storage_capacity(bound_workspace)
        steps = plan.plan.get("skill_plan", {}).get("steps", [])
        planned_steps = [
            {
                "step_id": item.get("step_id"),
                "capability": item.get("capability"),
            }
            for item in steps
            if isinstance(item, dict)
        ]
        preflight = {
            "ready": True,
            "plan_revision_id": plan.plan_revision_id,
            "plan_hash": plan.plan_hash,
            "manifest_hash": plan.manifest_hash,
            "workspace": {
                "path": str(bound_workspace),
                "total_space_bytes": total_space_bytes,
                "free_space_bytes": free_space_bytes,
            },
            "planned_steps": planned_steps,
        }
        payload = {"run_preflight": preflight}
        message = (
            "运行前复核通过。"
            f"当前磁盘可用空间为 {free_space_bytes / (1024**3):.1f} GiB；"
            f"计划包含 {len(planned_steps)} 个受控步骤。"
        )
        return WorkActionResult(
            assistant_content=message,
            payload=payload,
            tool=self._tool_result(
                "preview_dpabi_preprocessing",
                {
                    "project_id": project_id,
                    "plan_revision_id": plan.plan_revision_id,
                    "workspace_path": str(bound_workspace),
                },
                payload,
            ),
            workspace_path=str(bound_workspace),
            project_id=project_id,
        )

    def _start_preprocessing(
        self,
        request: ConversationTurnCreate,
        *,
        workspace_path: str | None,
        project_id: str | None,
        idempotency_key: str,
    ) -> WorkActionResult:
        missing = [
            name
            for name, value in (
                ("project_id", project_id),
                ("plan_revision_id", request.plan_revision_id),
                ("expected_plan_hash", request.expected_plan_hash),
            )
            if value is None
        ]
        if missing or not request.real_execution_confirmed:
            return WorkActionResult(
                assistant_content=(
                    "启动前需要已验证并审批的计划，以及本次真实 MATLAB/DPABI 运行确认。"
                    "确认后，任务会进入现有 Workflow/Worker 队列，"
                    "并在所选工作区原位生成 DPABI 结果目录。"
                ),
                tool={
                    "tool_name": "start_dpabi_preprocessing",
                    "status": ConversationToolStatus.AWAITING_CONFIRMATION.value,
                    "input": {"workspace_path": workspace_path, "missing": missing},
                    "output": {},
                },
                workspace_path=workspace_path,
            )
        assert project_id is not None
        assert request.plan_revision_id is not None
        assert request.expected_plan_hash is not None
        project = self._repository.get_project(project_id)
        plan = self._repository.get_plan(request.plan_revision_id)
        if plan.project_id != project_id:
            raise ConflictError("cross_project_plan", "审批计划不属于当前项目。")
        dataset_ref = plan.plan.get("skill_plan", {}).get("dataset_ref")
        if not isinstance(dataset_ref, str):
            raise InputValidationError(
                "preprocessing_skill_plan_required",
                "原位 DPABI 运行只接受已编译的预处理 SkillPlan。",
            )
        dataset = self._repository.get_dataset(dataset_ref)
        bound_workspace = self._path_policy.validate_read_path(
            dataset.source_path,
            project_roots=project.source_roots,
            expect_directory=True,
        )
        self._validate_workspace_match(workspace_path, bound_workspace, project.source_roots)
        run = self._create_run(
            RunCreate(
                project_id=project_id,
                plan_revision_id=request.plan_revision_id,
                expected_plan_hash=request.expected_plan_hash,
                execution_backend=ExecutionBackend.MATLAB,
                workspace_mode=ExecutionWorkspaceMode.IN_PLACE,
                real_execution_confirmed=True,
            ),
            f"{idempotency_key}:run",
        )
        payload = {"run": run.model_dump(mode="json")}
        return WorkActionResult(
            assistant_content=(
                f"DPABI 任务已进入 Workflow/Worker 队列，运行 ID 为 {run.run_id}。"
                "结果会写入该计划绑定的数据集工作区；可以继续询问运行进度。"
            ),
            payload=payload,
            tool=self._tool_result(
                "start_dpabi_preprocessing",
                {
                    "project_id": project_id,
                    "plan_revision_id": request.plan_revision_id,
                    "workspace_mode": "in_place",
                },
                payload,
            ),
            workspace_path=str(bound_workspace),
            project_id=project_id,
            active_run_id=run.run_id,
        )

    def _validate_workspace_match(
        self,
        workspace_path: str | None,
        bound_workspace: Any,
        project_roots: list[str],
    ) -> None:
        if workspace_path is None:
            return
        selected_workspace = self._path_policy.validate_read_path(
            workspace_path,
            project_roots=project_roots,
            expect_directory=True,
        )
        if selected_workspace != bound_workspace:
            raise ConflictError(
                "conversation_workspace_plan_mismatch",
                "当前对话选择的工作区与审批计划绑定的数据集不一致。",
            )

    @staticmethod
    def _resolve_action(
        request: ConversationTurnCreate,
        workspace_path: str | None,
        active_run_id: str | None,
    ) -> ConversationAction:
        if request.action is not ConversationAction.AUTO:
            return request.action
        text = request.content.casefold()
        if any(word in text for word in ("qc", "质量控制", "质控")):
            return ConversationAction.GET_QC_STATUS
        if any(word in text for word in ("统计结果", "结果报告", "显著簇")):
            return ConversationAction.GET_STATISTICAL_RESULTS
        if any(word in text for word in ("进度", "状态", "运行到", "完成了吗")):
            return ConversationAction.GET_PROGRESS
        if any(word in text for word in ("生成方案", "准备方案", "创建方案", "预处理方案")):
            return ConversationAction.PREPARE_PREPROCESSING_PLAN
        if any(word in text for word in ("启动", "运行", "执行", "开始预处理")):
            return ConversationAction.START_PREPROCESSING
        if workspace_path and any(word in text for word in ("建立项目", "注册工作区", "冻结清单")):
            return ConversationAction.SETUP_WORKSPACE
        return ConversationAction.AUTO

    @staticmethod
    def _tool_name(action: ConversationAction) -> str:
        return {
            ConversationAction.SETUP_WORKSPACE: "setup_workspace",
            ConversationAction.PREPARE_PREPROCESSING_PLAN: "prepare_preprocessing_plan",
            ConversationAction.PREVIEW_PREPROCESSING_RUN: "preview_dpabi_preprocessing",
            ConversationAction.GET_PROGRESS: "get_run_progress",
            ConversationAction.GET_QC_STATUS: "get_qc_status",
            ConversationAction.GET_STATISTICAL_RESULTS: "get_statistical_results",
            ConversationAction.START_PREPROCESSING: "start_dpabi_preprocessing",
        }.get(action, "conversation_work_request")


__all__ = ["ConversationWorkCoordinator", "WorkActionResult"]
