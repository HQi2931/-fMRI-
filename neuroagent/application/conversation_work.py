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
    ExecutionBackend,
    ExecutionWorkspaceMode,
    RunCreate,
    RunView,
    WorkspaceCheckRequest,
    WorkspaceCheckView,
)
from neuroagent.application.errors import ApplicationError, ConflictError, InputValidationError
from neuroagent.application.ports import PathPolicyPort, RepositoryPort


@dataclass(frozen=True, slots=True)
class WorkActionResult:
    assistant_content: str
    payload: dict[str, Any] = field(default_factory=dict)
    tool: dict[str, Any] | None = None
    workspace_path: str | None = None
    active_run_id: str | None = None


class ConversationWorkCoordinator:
    """Translate a Work request into one safe application action."""

    def __init__(
        self,
        *,
        repository: RepositoryPort,
        path_policy: PathPolicyPort,
        check_workspace: Callable[[WorkspaceCheckRequest], WorkspaceCheckView],
        get_run: Callable[[str], RunView],
        create_run: Callable[[RunCreate, str], RunView],
        tool_result: Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]],
    ) -> None:
        self._repository = repository
        self._path_policy = path_policy
        self._check_workspace = check_workspace
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
            if action is ConversationAction.CHECK_WORKSPACE:
                return self._check(workspace_path)
            if action is ConversationAction.GET_PROGRESS:
                return self._progress(active_run_id)
            if action is ConversationAction.START_PREPROCESSING:
                return self._start_preprocessing(
                    request,
                    workspace_path=workspace_path,
                    project_id=project_id,
                    idempotency_key=idempotency_key,
                )
            return WorkActionResult(
                "我已记录这条工作要求。你可以让我检查工作区、查看运行进度，"
                "或在已有审核计划后明确启动 DPABI 预处理。",
                workspace_path=workspace_path,
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
                active_run_id=active_run_id,
            )

    def _check(self, workspace_path: str | None) -> WorkActionResult:
        if workspace_path is None:
            return WorkActionResult("请先点击“浏览”并选择工作区。")
        checked = self._check_workspace(WorkspaceCheckRequest(path=workspace_path))
        payload = {"workspace_check": checked.model_dump(mode="json")}
        if checked.blocking_issues:
            message = (
                "工作区检查完成，发现 "
                f"{len(checked.blocking_issues)} 个阻断问题。"
                "请先按右侧列表修正，再准备预处理。"
            )
        else:
            message = (
                "工作区检查通过：识别到 "
                f"{checked.functional_subject_count} 名受试者的功能输入。"
                "你可以继续准备已审核的预处理方案。"
            )
        return WorkActionResult(
            assistant_content=message,
            payload=payload,
            tool=self._tool_result("check_workspace", {"path": checked.path}, payload),
            workspace_path=checked.path,
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
        if any(word in text for word in ("进度", "状态", "运行到", "完成了吗")):
            return ConversationAction.GET_PROGRESS
        if any(word in text for word in ("启动", "运行", "执行", "开始预处理")):
            return ConversationAction.START_PREPROCESSING
        if workspace_path and any(word in text for word in ("检查", "扫描", "格式", "dpabi")):
            return ConversationAction.CHECK_WORKSPACE
        if workspace_path and active_run_id is None:
            return ConversationAction.CHECK_WORKSPACE
        return ConversationAction.AUTO

    @staticmethod
    def _tool_name(action: ConversationAction) -> str:
        return {
            ConversationAction.CHECK_WORKSPACE: "check_workspace",
            ConversationAction.GET_PROGRESS: "get_run_progress",
            ConversationAction.START_PREPROCESSING: "start_dpabi_preprocessing",
        }.get(action, "conversation_work_request")


__all__ = ["ConversationWorkCoordinator", "WorkActionResult"]
