"""Narrow application use cases for durable user-goal progress."""

# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from neuroagent.application.automation.models import (
    AuthoritativeRunSnapshot,
    EventConsumptionDecision,
    EventReconciliationReceipt,
    RunReadiness,
    TaskBudget,
    TaskStatus,
    TaskStep,
    TaskStepStatus,
    WaitingCondition,
    WaitingKind,
    WorkEventIdentity,
    WorkflowRunReconciliation,
    WorkGoal,
    WorkTask,
    transition_task,
    transition_task_step,
)
from neuroagent.application.automation.ports import WorkTaskRepositoryPort
from neuroagent.application.contracts.conversations import ConversationMode
from neuroagent.application.contracts.statistics import (
    StatisticalResultDetailView,
    StatisticalResultView,
)
from neuroagent.application.errors import (
    ConflictError,
    InputValidationError,
    NotFoundError,
)
from neuroagent.application.hashing import content_hash
from neuroagent.application.repository_ports import (
    ArtifactCatalogRepositoryPort,
    ConversationRepositoryPort,
    EventRepositoryPort,
    PlanRepositoryPort,
    QcRepositoryPort,
    RunRepositoryPort,
)
from neuroagent.application.statistical_result_facts import verified_result_design
from neuroagent.domain.fmri.results import StatisticalResultMode
from neuroagent.domain.fmri.statistics import StatisticalDesignRevision
from neuroagent.domain.lifecycle import PlanState, WorkflowState


class _InvalidStatisticalAuthority(ValueError):
    """Raised when a stored statistical task/result chain cannot be trusted."""


class WorkTaskApplicationService:
    """Persist task intent and event receipts without owning scientific run state."""

    def __init__(
        self,
        repository: WorkTaskRepositoryPort,
        *,
        event_repository: EventRepositoryPort,
        run_repository: RunRepositoryPort,
        qc_repository: QcRepositoryPort,
        artifact_repository: ArtifactCatalogRepositoryPort,
        conversation_repository: ConversationRepositoryPort,
        plan_repository: PlanRepositoryPort,
    ) -> None:
        self._repository = repository
        self._event_repository = event_repository
        self._run_repository = run_repository
        self._qc_repository = qc_repository
        self._artifact_repository = artifact_repository
        self._conversation_repository = conversation_repository
        self._plan_repository = plan_repository

    def create_task(self, task: WorkTask) -> WorkTask:
        return self._repository.create_task(task)

    def get_task(self, task_id: str) -> WorkTask:
        return self._repository.get_task(task_id)

    def reconcile_next_due_task(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
        cooldown_seconds: float,
    ) -> WorkflowRunReconciliation | None:
        """Claim and reconcile one due Run wait; the lease only fences task writes."""

        claim = self._repository.claim_next_reconciliation_task(
            worker_id,
            lease_seconds=lease_seconds,
            cooldown_seconds=cooldown_seconds,
        )
        if claim is None:
            return None
        try:
            return self.reconcile_waiting_run(claim.task_id, claim_token=claim.claim_token)
        finally:
            self._repository.release_reconciliation_claim(
                claim.task_id,
                claim.worker_id,
                claim.claim_token,
            )

    @staticmethod
    def analysis_proposal_task_id(conversation_id: str, proposal_hash: str) -> str:
        binding = f"{conversation_id}\0analysis\0{proposal_hash}".encode()
        return f"work-analysis-{hashlib.sha256(binding).hexdigest()[:32]}"

    def ensure_analysis_proposal_task(
        self,
        conversation_id: str,
        project_id: str,
        proposal_hash: str,
        goal_description: str,
        draft_hash: str,
    ) -> WorkTask:
        """Create/reuse the durable task for one reviewed statistical proposal."""

        self._validate_hash(proposal_hash, "proposal_hash")
        self._validate_hash(draft_hash, "draft_hash")
        conversation = self._conversation_repository.get_conversation(conversation_id)
        self._require_work_conversation(conversation, conversation_id, project_id)
        description = goal_description.strip()[:4000]
        if not description:
            raise InputValidationError("work_task_goal_required", "持久任务缺少用户目标描述。")

        task_id = self.analysis_proposal_task_id(conversation_id, proposal_hash)
        task = WorkTask(
            task_id=task_id,
            goal=WorkGoal(
                description=description,
                completion_conditions=(
                    "统计 Run 已成功，并且存在唯一、可复核、与冻结统计设计和 Plan 哈希一致的"
                    "真实 StatisticalResult；零显著体素也是有效结果。",
                ),
            ),
            status=TaskStatus.WAITING_USER,
            waiting_condition=WaitingCondition(
                kind=WaitingKind.USER_DECISION,
                reference=f"proposal:{proposal_hash}",
                reason="统计提案已生成，等待用户审查并确认冻结分析方案。",
            ),
            authority_refs={
                "conversation_id": conversation_id,
                "input_revision": draft_hash,
            },
            steps=(
                TaskStep(
                    step_id=f"statistical:{proposal_hash}",
                    sequence=0,
                    kind="statistical_run",
                    status=TaskStepStatus.READY,
                    input_version=proposal_hash,
                    authority_refs={
                        "conversation_id": conversation_id,
                        "input_revision": draft_hash,
                    },
                ),
            ),
            budget=TaskBudget(
                max_steps=100,
                max_model_calls=40,
                max_tokens=200_000,
                max_elapsed_seconds=86_400,
            ),
        )
        try:
            current = self._repository.get_task(task_id)
        except NotFoundError:
            return self._repository.create_task(task)
        current_step = self._statistical_step(current)
        if (
            current.authority_refs.conversation_id != conversation_id
            or current.authority_refs.input_revision != draft_hash
            or current_step is None
            or current_step.input_version != proposal_hash
            or current_step.authority_refs.conversation_id != conversation_id
            or current_step.authority_refs.input_revision != draft_hash
        ):
            raise ConflictError(
                "work_task_binding_conflict", "同一统计提案已绑定到不同草稿或会话。"
            )
        return current

    def bind_statistical_plan(
        self,
        task_id: str,
        conversation_id: str,
        project_id: str,
        proposal_hash: str,
        plan_revision_id: str,
    ) -> WorkTask:
        """Bind the exact frozen statistical Plan while keeping the user gate."""

        expected_task_id = self.analysis_proposal_task_id(conversation_id, proposal_hash)
        if task_id != expected_task_id:
            raise ConflictError("work_task_binding_mismatch", "统计任务 ID 与提案绑定不一致。")
        conversation = self._conversation_repository.get_conversation(conversation_id)
        self._require_work_conversation(conversation, conversation_id, project_id)
        plan = self._plan_repository.get_plan(plan_revision_id)
        if plan.project_id != project_id:
            raise ConflictError("work_task_plan_project_mismatch", "统计方案不属于当前项目。")
        context, design = self._statistical_plan_context(
            plan,
            conversation_id=conversation_id,
            project_id=project_id,
            proposal_hash=proposal_hash,
        )
        if plan.state not in {
            PlanState.DRAFT,
            PlanState.AWAITING_APPROVAL,
            PlanState.APPROVED,
        }:
            raise ConflictError("work_task_plan_not_reviewable", "统计方案尚未冻结供用户审查。")

        current = self._repository.get_task(task_id)
        step = self._statistical_step(current)
        if (
            current.task_id != expected_task_id
            or current.authority_refs.conversation_id != conversation_id
            or current.authority_refs.input_revision != context["draft_hash"]
            or step is None
            or step.input_version != proposal_hash
            or step.authority_refs.conversation_id != conversation_id
            or step.authority_refs.input_revision != context["draft_hash"]
        ):
            raise ConflictError("work_task_binding_mismatch", "统计任务与当前提案草稿不匹配。")
        if current.authority_refs.plan_revision_id not in {None, plan_revision_id}:
            raise ConflictError("work_task_plan_already_bound", "统计任务已绑定另一冻结方案。")
        if step.authority_refs.plan_revision_id not in {None, plan_revision_id}:
            raise ConflictError("work_task_plan_already_bound", "统计步骤已绑定另一冻结方案。")
        if current.authority_refs.plan_revision_id == plan_revision_id:
            return current
        if current.status is not TaskStatus.WAITING_USER or step.status is not TaskStepStatus.READY:
            raise ConflictError("work_task_not_waiting_for_plan", "统计任务当前不能绑定新方案。")

        updated_step = step.model_copy(
            update={
                "authority_refs": step.authority_refs.model_copy(
                    update={"plan_revision_id": plan_revision_id}
                ),
            }
        )
        updated = WorkTask.model_validate(
            {
                **current.model_dump(),
                "version": current.version + 1,
                "authority_refs": current.authority_refs.model_copy(
                    update={"plan_revision_id": plan_revision_id}
                ).model_dump(),
                "waiting_condition": WaitingCondition(
                    kind=WaitingKind.USER_DECISION,
                    reference=f"plan:{plan_revision_id}",
                    reason=(f"统计方案已冻结，设计 {design.revision_id}；等待用户审查并确认启动。"),
                ).model_dump(),
                "steps": tuple(
                    updated_step if item.step_id == step.step_id else item for item in current.steps
                ),
            }
        )
        return self._repository.save_task(updated, expected_version=current.version)

    def bind_statistical_run(
        self,
        task_id: str,
        conversation_id: str,
        project_id: str,
        plan_revision_id: str,
        run_id: str,
    ) -> WorkTask:
        """Bind the Run created by the user's authorized statistics action."""

        conversation = self._conversation_repository.get_conversation(conversation_id)
        self._require_work_conversation(conversation, conversation_id, project_id)
        plan = self._plan_repository.get_plan(plan_revision_id)
        if plan.project_id != project_id:
            raise ConflictError("work_task_plan_project_mismatch", "统计方案不属于当前项目。")
        task = self._repository.get_task(task_id)
        step = self._statistical_step(task)
        if step is None or task.authority_refs.conversation_id != conversation_id:
            raise ConflictError("work_task_binding_mismatch", "统计任务与当前会话不匹配。")
        proposal_hash = step.input_version
        if not proposal_hash:
            raise ConflictError("work_task_binding_mismatch", "统计任务缺少已审查提案哈希。")
        context, _design = self._statistical_plan_context(
            plan,
            conversation_id=conversation_id,
            project_id=project_id,
            proposal_hash=proposal_hash,
        )
        if plan.state is not PlanState.APPROVED:
            raise ConflictError(
                "work_task_plan_not_approved", "只能将已批准并授权执行的统计方案绑定到 Run。"
            )
        if (
            task.task_id != self.analysis_proposal_task_id(conversation_id, proposal_hash)
            or task.authority_refs.input_revision != context["draft_hash"]
            or task.authority_refs.plan_revision_id != plan_revision_id
            or step.authority_refs.conversation_id != conversation_id
            or step.authority_refs.input_revision != context["draft_hash"]
            or step.authority_refs.plan_revision_id != plan_revision_id
        ):
            raise ConflictError("work_task_binding_mismatch", "统计任务与冻结方案不匹配。")
        run = self._run_repository.get_run(run_id)
        if run.project_id != project_id or run.plan_revision_id != plan_revision_id:
            raise ConflictError("work_task_run_plan_mismatch", "统计 Run 与冻结方案或项目不匹配。")
        if task.authority_refs.workflow_run_id is not None:
            if (
                task.authority_refs.workflow_run_id == run_id
                and step.authority_refs.workflow_run_id == run_id
                and (
                    (
                        task.status is TaskStatus.WAITING_RUN
                        and step.status is TaskStepStatus.WAITING
                    )
                    or (
                        task.status is TaskStatus.WAITING_DEPENDENCY
                        and step.status is TaskStepStatus.WAITING
                    )
                    or (
                        task.status is TaskStatus.COMPLETED
                        and step.status is TaskStepStatus.SUCCEEDED
                    )
                )
            ):
                return task
            raise ConflictError("work_task_run_already_bound", "统计任务已绑定另一 Run。")
        if task.status is not TaskStatus.WAITING_USER or step.status is not TaskStepStatus.READY:
            raise ConflictError("work_task_not_waiting_for_start", "统计任务当前不在等待启动状态。")
        started = transition_task_step(step, TaskStepStatus.RUNNING)
        waiting_step = TaskStep.model_validate(
            {
                **transition_task_step(started, TaskStepStatus.WAITING).model_dump(),
                "authority_refs": started.authority_refs.model_copy(
                    update={"workflow_run_id": run_id}
                ).model_dump(),
            }
        )
        updated = transition_task(
            task,
            TaskStatus.WAITING_RUN,
            waiting_condition=WaitingCondition(
                kind=WaitingKind.WORKFLOW_RUN,
                reference=run_id,
                reason="用户已确认启动统计分析；等待该统计 Workflow Run 和结果登记。",
            ),
        ).model_copy(
            update={
                "authority_refs": task.authority_refs.model_copy(
                    update={"workflow_run_id": run_id}
                ),
                "steps": tuple(
                    waiting_step if item.step_id == step.step_id else item for item in task.steps
                ),
            }
        )
        return self._repository.save_task(
            WorkTask.model_validate(updated.model_dump()), expected_version=task.version
        )

    @staticmethod
    def _validate_hash(value: str, field: str) -> None:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise InputValidationError("work_task_hash_invalid", f"{field} 必须是 SHA-256 哈希。")

    @staticmethod
    def _require_work_conversation(
        conversation: Any, conversation_id: str, project_id: str
    ) -> None:
        if conversation.mode is not ConversationMode.WORK:
            raise InputValidationError(
                "work_task_conversation_mode_required", "持久统计任务需要 Work 会话。"
            )
        if conversation.conversation_id != conversation_id:
            raise ConflictError("work_task_conversation_mismatch", "会话读取结果与请求 ID 不一致。")
        if conversation.project_id not in {None, project_id}:
            raise ConflictError("work_task_project_mismatch", "Work 会话已绑定其他项目。")

    @staticmethod
    def _statistical_step(task: WorkTask) -> TaskStep | None:
        steps = tuple(step for step in task.steps if step.kind == "statistical_run")
        return steps[0] if len(steps) == 1 else None

    @staticmethod
    def _statistical_plan_context(
        plan: Any,
        *,
        conversation_id: str,
        project_id: str,
        proposal_hash: str,
    ) -> tuple[dict[str, Any], StatisticalDesignRevision]:
        context = plan.plan.get("work_context")
        raw_design = plan.plan.get("design")
        if (
            plan.project_id != project_id
            or plan.plan.get("kind") != "statistical_design"
            or not isinstance(context, dict)
            or not isinstance(raw_design, dict)
            or context.get("conversation_id") != conversation_id
            or context.get("proposal_hash") != proposal_hash
            or not isinstance(context.get("draft_hash"), str)
        ):
            raise ConflictError(
                "work_task_statistical_plan_mismatch", "统计方案 work_context 与提案任务不匹配。"
            )
        WorkTaskApplicationService._validate_hash(context["draft_hash"], "draft_hash")
        try:
            design = StatisticalDesignRevision.model_validate(raw_design)
        except ValidationError as exc:
            raise ConflictError(
                "work_task_statistical_plan_invalid", "冻结统计方案中的 design 无法核验。"
            ) from exc
        expected_plan_hash = content_hash(
            {
                "plan": plan.plan,
                "manifest_hash": plan.manifest_hash,
                "environment_hash": plan.environment_hash,
            }
        )
        if expected_plan_hash != plan.plan_hash:
            raise ConflictError(
                "work_task_statistical_plan_hash_mismatch",
                "统计方案内容与冻结 Plan 哈希不一致。",
            )
        return context, design

    @staticmethod
    def preprocessing_task_id(conversation_id: str, plan_revision_id: str) -> str:
        """Return a stable key for one preprocessing Plan in one Work conversation."""

        binding = f"{conversation_id}\0preprocessing\0{plan_revision_id}".encode()
        return f"work-preprocessing-{hashlib.sha256(binding).hexdigest()[:32]}"

    def ensure_preprocessing_plan_task(
        self,
        *,
        conversation_id: str,
        project_id: str,
        plan_revision_id: str,
        goal_description: str,
    ) -> WorkTask:
        """Persist a user-reviewed preprocessing goal after its Plan commits.

        The caller runs this from the Work turn's final SQLite unit of work.
        Repeated calls for the same conversation/Plan are idempotent; a stable
        key that resolves to different authority references is treated as a
        binding conflict.
        """

        conversation = self._conversation_repository.get_conversation(conversation_id)
        if conversation.mode is not ConversationMode.WORK:
            raise InputValidationError(
                "work_task_conversation_mode_required", "持久预处理任务需要 Work 会话。"
            )
        if conversation.project_id not in {None, project_id}:
            raise ConflictError(
                "work_task_project_mismatch", "Work 会话已绑定其他项目，不能关联此预处理方案。"
            )
        plan = self._plan_repository.get_plan(plan_revision_id)
        if plan.project_id != project_id:
            raise ConflictError(
                "work_task_plan_project_mismatch", "预处理方案不属于当前 Work 项目。"
            )
        if plan.state not in {PlanState.AWAITING_APPROVAL, PlanState.APPROVED}:
            raise ConflictError(
                "work_task_plan_not_reviewable", "只有成功冻结的预处理方案可以创建持久任务。"
            )
        skill_plan = plan.plan.get("skill_plan")
        if plan.plan.get("kind") != "skill_plan" or not isinstance(skill_plan, dict):
            raise ConflictError(
                "work_task_preprocessing_plan_required", "当前方案不是可追溯的预处理 SkillPlan。"
            )
        if not isinstance(skill_plan.get("preprocessing_parameters_hash"), str):
            raise ConflictError(
                "work_task_preprocessing_plan_required", "当前 SkillPlan 未请求预处理。"
            )
        description = goal_description.strip()[:4000]
        if not description:
            raise InputValidationError("work_task_goal_required", "持久任务缺少用户目标描述。")

        task_id = self.preprocessing_task_id(conversation_id, plan_revision_id)
        outputs = tuple(
            str(value)
            for value in skill_plan.get("primary_outputs", ())
            if isinstance(value, str) and value.strip()
        )
        metrics = tuple(
            str(value)
            for value in skill_plan.get("requested_metrics", ())
            if isinstance(value, str) and value.strip()
        )
        expectations = tuple(
            str(value)
            for value in skill_plan.get("artifact_expectations", ())
            if isinstance(value, str) and value.strip()
        )
        targets = outputs or metrics or expectations
        completion_conditions = [
            "该 Plan 对应的 Workflow Run 达到 succeeded。",
            "用户启动确认选择的 QC 要求由权威记录满足：要求 QC 时存在 approved 审查；"
            "明确跳过时存在 QC skip 记录。",
        ]
        if targets:
            completion_conditions.append(
                "该 Run 已登记此 Plan 要求的目标产物：" + "、".join(targets) + "。"
            )
        else:
            completion_conditions.append("该 Run 已登记该预处理 Plan 声明的目标产物。")

        task = WorkTask(
            task_id=task_id,
            goal=WorkGoal(
                description=description,
                completion_conditions=tuple(completion_conditions),
            ),
            status=TaskStatus.WAITING_USER,
            waiting_condition=WaitingCondition(
                kind=WaitingKind.USER_DECISION,
                reference=f"plan:{plan_revision_id}",
                reason="预处理方案已提交，等待用户审查并明确启动。",
            ),
            authority_refs={
                "conversation_id": conversation_id,
                "plan_revision_id": plan_revision_id,
                "input_revision": plan.manifest_hash,
            },
            steps=(
                TaskStep(
                    step_id=f"preprocessing:{plan_revision_id}",
                    sequence=0,
                    kind="preprocessing_run",
                    status=TaskStepStatus.READY,
                    input_version=plan.plan_hash,
                    authority_refs={
                        "conversation_id": conversation_id,
                        "plan_revision_id": plan_revision_id,
                        "input_revision": plan.manifest_hash,
                    },
                ),
            ),
            budget=TaskBudget(
                max_steps=100,
                max_model_calls=40,
                max_tokens=200_000,
                max_elapsed_seconds=86_400,
            ),
        )
        try:
            current = self._repository.get_task(task_id)
        except NotFoundError:
            return self._repository.create_task(task)
        if (
            current.authority_refs.conversation_id != conversation_id
            or current.authority_refs.plan_revision_id != plan_revision_id
            or current.authority_refs.input_revision not in {None, plan.manifest_hash}
        ):
            raise ConflictError(
                "work_task_binding_conflict", "同一 Plan 已绑定到其他会话的持久任务。"
            )
        if current.authority_refs.input_revision is None:
            updated = current.model_copy(
                update={
                    "version": current.version + 1,
                    "authority_refs": current.authority_refs.model_copy(
                        update={"input_revision": plan.manifest_hash}
                    ),
                    "steps": tuple(
                        step.model_copy(
                            update={
                                "authority_refs": step.authority_refs.model_copy(
                                    update={"input_revision": plan.manifest_hash}
                                )
                            }
                        )
                        for step in current.steps
                    ),
                }
            )
            return self._repository.save_task(updated, expected_version=current.version)
        return current

    def bind_preprocessing_run(
        self,
        *,
        task_id: str,
        conversation_id: str,
        project_id: str,
        plan_revision_id: str,
        run_id: str,
    ) -> WorkTask:
        """Bind a successfully queued Run to its existing conversation goal."""

        conversation = self._conversation_repository.get_conversation(conversation_id)
        if conversation.mode is not ConversationMode.WORK:
            raise InputValidationError(
                "work_task_conversation_mode_required", "持久预处理任务需要 Work 会话。"
            )
        if conversation.project_id not in {None, project_id}:
            raise ConflictError(
                "work_task_project_mismatch", "Work 会话已绑定其他项目，不能关联此运行。"
            )
        plan = self._plan_repository.get_plan(plan_revision_id)
        run = self._run_repository.get_run(run_id)
        skill_plan = plan.plan.get("skill_plan")
        if plan.plan.get("kind") != "skill_plan" or not isinstance(skill_plan, dict):
            raise ConflictError(
                "work_task_preprocessing_plan_required", "运行没有对应的预处理 SkillPlan。"
            )
        if not isinstance(skill_plan.get("preprocessing_parameters_hash"), str):
            raise ConflictError(
                "work_task_preprocessing_plan_required", "指标计算方案不能绑定为预处理任务。"
            )
        if plan.project_id != project_id or run.project_id != project_id:
            raise ConflictError(
                "work_task_run_project_mismatch", "运行或方案不属于当前 Work 项目。"
            )
        if run.plan_revision_id != plan_revision_id:
            raise ConflictError(
                "work_task_run_plan_mismatch", "运行没有使用当前 Work 任务绑定的预处理方案。"
            )
        task = self._repository.get_task(task_id)
        if (
            task.authority_refs.conversation_id != conversation_id
            or task.authority_refs.plan_revision_id != plan_revision_id
            or task.authority_refs.input_revision not in {None, plan.manifest_hash}
        ):
            raise ConflictError("work_task_binding_mismatch", "持久任务与当前会话或方案不匹配。")
        expected_id = self.preprocessing_task_id(conversation_id, plan_revision_id)
        if task_id != expected_id:
            raise ConflictError("work_task_binding_mismatch", "持久任务 ID 与权威绑定不匹配。")
        if task.authority_refs.workflow_run_id is not None:
            if task.authority_refs.workflow_run_id == run_id:
                return task
            raise ConflictError(
                "work_task_run_already_bound", "该预处理任务已经绑定另一 Workflow Run。"
            )
        if task.status is not TaskStatus.WAITING_USER:
            raise ConflictError(
                "work_task_not_waiting_for_start", "持久任务当前不在等待用户启动的状态。"
            )

        step = next(
            (
                item
                for item in task.steps
                if item.kind == "preprocessing_run"
                and item.authority_refs.plan_revision_id == plan_revision_id
            ),
            None,
        )
        if step is None or step.status is not TaskStepStatus.READY:
            raise ConflictError("work_task_step_not_ready", "预处理任务步骤尚未准备启动。")
        running_step = transition_task_step(step, TaskStepStatus.RUNNING)
        waiting_step = TaskStep.model_validate(
            {
                **transition_task_step(running_step, TaskStepStatus.WAITING).model_dump(),
                "authority_refs": running_step.authority_refs.model_copy(
                    update={"workflow_run_id": run_id}
                ).model_dump(),
            }
        )
        updated = transition_task(
            task,
            TaskStatus.WAITING_RUN,
            waiting_condition=WaitingCondition(
                kind=WaitingKind.WORKFLOW_RUN,
                reference=run_id,
                reason="用户已确认启动；等待该预处理 Workflow Run 完成。",
            ),
        ).model_copy(
            update={
                "authority_refs": task.authority_refs.model_copy(
                    update={"workflow_run_id": run_id}
                ),
                "steps": tuple(
                    waiting_step if item.step_id == step.step_id else item for item in task.steps
                ),
                "goal": WorkGoal(
                    description=task.goal.description,
                    completion_conditions=(
                        *tuple(
                            condition
                            for condition in task.goal.completion_conditions
                            if not condition.startswith("用户启动确认选择的 QC 要求")
                        ),
                        "该 Run 存在 approved 的头动 QC 审查记录。"
                        if run.qc_mode.value == "require"
                        else "该 Run 存在用户明确作出的 QC skip 记录。",
                    ),
                ),
            }
        )
        updated = WorkTask.model_validate(updated.model_dump())
        return self._repository.save_task(updated, expected_version=task.version)

    def transition_task(
        self,
        task_id: str,
        *,
        expected_version: int,
        target: TaskStatus,
        waiting_condition: WaitingCondition | None = None,
    ) -> WorkTask:
        current = self._repository.get_task(task_id)
        if current.version != expected_version:
            raise ConflictError(
                "revision_conflict",
                "自动化任务已更新。请重新读取后再继续。",
                resource="work_task",
                resource_id=task_id,
                expected=expected_version,
                actual=current.version,
            )
        updated = transition_task(
            current,
            target,
            waiting_condition=waiting_condition,
        )
        return self._repository.save_task(updated, expected_version=expected_version)

    def consume_workflow_event(
        self,
        task_id: str,
        event: WorkEventIdentity,
        *,
        claim_token: str | None = None,
    ) -> tuple[EventConsumptionDecision, WorkTask]:
        """Record a workflow notification once; workflow truth is reloaded elsewhere.

        This method deliberately does not interpret event payloads or copy Run,
        QC, or Artifact status. A successful receipt only advances the task's
        orchestration version and cursor so a later coordinator can reread the
        authoritative workflow records before deciding the next action.
        """

        current = self._repository.get_task(task_id)
        expected_run_id = current.authority_refs.workflow_run_id
        if expected_run_id is None or event.workflow_run_id != expected_run_id:
            raise InputValidationError(
                "workflow_event_run_mismatch",
                "运行事件与自动化任务绑定的运行不匹配。",
                task_id=task_id,
                expected_run_id=expected_run_id,
                event_run_id=event.workflow_run_id,
            )
        expected_stream_key = f"workflow:{expected_run_id}"
        if event.stream_key != expected_stream_key:
            raise InputValidationError(
                "workflow_event_stream_mismatch",
                "运行事件不属于该任务的标准 Workflow 事件游标。",
                task_id=task_id,
                expected_stream_key=expected_stream_key,
                event_stream_key=event.stream_key,
            )
        candidate = WorkTask.model_validate(
            {**current.model_dump(), "version": current.version + 1}
        )
        decision, _committed = self._repository.consume_event(
            task_id,
            event,
            expected_version=current.version,
            updated_task=candidate,
            claim_token=claim_token,
        )
        # Read after both applied and no-op decisions so a concurrent duplicate
        # returns current persisted state rather than a stale pre-read snapshot.
        return decision, self._repository.get_task(task_id)

    def reconcile_waiting_run(
        self,
        task_id: str,
        *,
        claim_token: str | None = None,
    ) -> WorkflowRunReconciliation:
        """Consume Run event identities, then reread Workflow, QC and Artifacts.

        Runtime event type and payload are deliberately ignored. Events only
        wake this use case and provide a stable identity for deduplication;
        readiness comes exclusively from the existing authoritative read ports.
        """

        task = self._repository.get_task(task_id)
        run_id = task.authority_refs.workflow_run_id
        if run_id is None:
            raise InputValidationError(
                "workflow_run_reference_missing",
                "自动化任务没有绑定可核验的运行。",
                task_id=task_id,
            )
        is_statistical = any(step.kind == "statistical_run" for step in task.steps)
        is_waiting_for_bound_run = (
            task.status is TaskStatus.WAITING_RUN
            and task.waiting_condition is not None
            and task.waiting_condition.reference == run_id
        )
        is_waiting_for_statistical_result = (
            is_statistical
            and task.status is TaskStatus.WAITING_DEPENDENCY
            and task.waiting_condition is not None
            and task.waiting_condition.reference == f"statistical-result:{run_id}"
        )
        is_waiting_for_bound_qc = (
            not is_statistical
            and task.status is TaskStatus.WAITING_USER
            and task.waiting_condition is not None
            and task.waiting_condition.reference == f"qc:{run_id}"
        )
        if not (
            is_waiting_for_bound_run or is_waiting_for_bound_qc or is_waiting_for_statistical_result
        ):
            raise InputValidationError(
                "task_not_waiting_for_workflow_run",
                "自动化任务当前并未等待该运行或其 QC 决定。",
                task_id=task_id,
                task_status=task.status.value,
            )

        stream_key = f"workflow:{run_id}"
        cursor = next(
            (item.event_id for item in task.event_cursors if item.stream_key == stream_key),
            0,
        )
        events = self._event_repository.list_events(
            run_id=run_id,
            after_event_id=cursor,
            limit=1000,
        )
        # Validate the entire returned batch before consuming any identity so a
        # broken adapter cannot partially advance the task with a wrong-Run event.
        for event in events:
            if event.run_id != run_id:
                raise InputValidationError(
                    "workflow_event_run_mismatch",
                    "事件仓储返回了与任务绑定运行不一致的事件。",
                    task_id=task_id,
                    expected_run_id=run_id,
                    event_run_id=event.run_id,
                    event_id=event.event_id,
                )

        receipts: list[EventReconciliationReceipt] = []
        for event in sorted(events, key=lambda item: item.event_id)[:1000]:
            identity = WorkEventIdentity(
                stream_key=stream_key,
                workflow_run_id=run_id,
                event_id=event.event_id,
                dedupe_key=f"runtime-event:{event.event_id}",
            )
            decision, task = self.consume_workflow_event(
                task_id,
                identity,
                claim_token=claim_token,
            )
            receipts.append(EventReconciliationReceipt(event_id=event.event_id, decision=decision))

        # Consume can race with user/API state changes. Always use the persisted
        # task version before deciding whether this reconciliation may transition it.
        task = self._repository.get_task(task_id)
        is_waiting_for_current_statistical_result = (
            self._has_statistical_step(task)
            and task.status is TaskStatus.WAITING_DEPENDENCY
            and task.waiting_condition is not None
            and task.waiting_condition.reference == f"statistical-result:{run_id}"
        )
        is_waiting_for_current_run = (
            task.status is TaskStatus.WAITING_RUN
            and task.waiting_condition is not None
            and task.waiting_condition.reference == run_id
        )
        is_waiting_for_current_qc = (
            not self._has_statistical_step(task)
            and task.status is TaskStatus.WAITING_USER
            and task.waiting_condition is not None
            and task.waiting_condition.reference == f"qc:{run_id}"
        )
        if not (
            is_waiting_for_current_statistical_result
            or is_waiting_for_current_run
            or is_waiting_for_current_qc
        ):
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=tuple(receipts),
                reason="任务等待状态已并发变化;本次只记录事件身份,需重新读取后再协调。",
            )

        if self._has_statistical_step(task):
            return self._reconcile_statistical_run(
                task,
                run_id,
                tuple(receipts),
                claim_token=claim_token,
            )

        try:
            snapshot = self._read_stable_authoritative_snapshot(task, run_id)
        except NotFoundError:
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=tuple(receipts),
                reason="绑定的 Run 或其权威记录暂不可读取;任务状态未推进。",
            )
        if snapshot is None:
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=tuple(receipts),
                reason=("Run 在三次权威状态读取期间都发生版本变化;保留任务原状态,稍后重新核验。"),
            )
        run_state = WorkflowState(snapshot.run_state)

        target_status: TaskStatus | None = None
        waiting_condition: WaitingCondition | None = None
        readiness = RunReadiness.INDETERMINATE
        reason = "当前 Workflow/QC 组合无法安全判定后续动作;保留任务状态供人工核验。"

        if snapshot.unresolved_referenced_artifact_ids:
            readiness = RunReadiness.BLOCKED
            target_status = TaskStatus.BLOCKED
            reason = "任务引用的 Artifact 不存在或不属于该项目;需先核验引用。"
        elif run_state in {
            WorkflowState.QUEUED,
            WorkflowState.RUNNING,
            WorkflowState.CANCELLING,
        }:
            readiness = RunReadiness.WAITING_RUN
            target_status = TaskStatus.WAITING_RUN
            waiting_condition = WaitingCondition(
                kind=WaitingKind.WORKFLOW_RUN,
                reference=run_id,
                reason="等待该 Workflow Run 进入可核验的终态。",
            )
            reason = "权威 Workflow 状态仍在排队、运行或取消中。"
        elif run_state is WorkflowState.QC_REVIEW:
            readiness = RunReadiness.WAITING_USER
            has_actionable_qc_review = (
                snapshot.qc_status in {"pending", "awaiting_review"}
                and snapshot.latest_qc_review_state in {None, "draft"}
            ) or (snapshot.qc_status == "failed" and snapshot.latest_qc_review_state == "draft")
            if has_actionable_qc_review:
                target_status = TaskStatus.WAITING_USER
                waiting_condition = WaitingCondition(
                    kind=WaitingKind.USER_DECISION,
                    reference=f"qc:{run_id}",
                    reason="Workflow 已到达 QC 决策边界;需要用户查看并明确处理 QC。",
                )
                reason = "QC 尚未明确批准或记录显式跳过;本次未创建或批准 QC。"
            else:
                readiness = RunReadiness.BLOCKED
                target_status = TaskStatus.BLOCKED
                reason = "Workflow 处于 QC_REVIEW,但 QC 状态或审查记录不一致。"
        elif run_state is WorkflowState.SUCCEEDED:
            if snapshot.qc_status == "approved" and snapshot.latest_qc_review_state == "approved":
                readiness = RunReadiness.WAITING_DEPENDENCY
                target_status = TaskStatus.WAITING_DEPENDENCY
                waiting_condition = WaitingCondition(
                    kind=WaitingKind.DEPENDENCY,
                    reference=f"downstream:{run_id}",
                    reason="运行与 QC 已核验;还需按目标检查所需 Artifact 和下一步骤。",
                )
                reason = (
                    "Workflow 已成功且权威 QC 审查为 approved;当前任务协议没有目标级"
                    " Artifact 完整性/下一步骤检查器,因此停在 waiting_dependency。"
                )
            elif snapshot.qc_status == "skipped" and snapshot.qc_skip_id is not None:
                readiness = RunReadiness.WAITING_DEPENDENCY
                target_status = TaskStatus.WAITING_DEPENDENCY
                waiting_condition = WaitingCondition(
                    kind=WaitingKind.DEPENDENCY,
                    reference=f"downstream:{run_id}",
                    reason="运行及显式 QC 跳过记录已核验;仍需检查目标级 Artifact 和下一步骤。",
                )
                reason = (
                    "Workflow 已成功且存在显式 QC 跳过记录;未隐含审批,仍需目标级"
                    " Artifact/下一步骤检查。"
                )
            elif snapshot.qc_status == "rejected" or snapshot.latest_qc_review_state == "rejected":
                readiness = RunReadiness.BLOCKED
                target_status = TaskStatus.BLOCKED
                reason = "权威 QC 记录为 rejected;任务已阻断,需用户决定后续处理。"
            elif snapshot.qc_status == "pending" and snapshot.latest_qc_review_state in {
                None,
                "draft",
            }:
                readiness = RunReadiness.WAITING_USER
                target_status = TaskStatus.WAITING_USER
                waiting_condition = WaitingCondition(
                    kind=WaitingKind.USER_DECISION,
                    reference=f"qc:{run_id}",
                    reason="运行已成功但 QC 仍 pending;等待用户检查或明确处理。",
                )
                reason = "运行成功但 QC 仍 pending;本次不会隐含批准或跳过。"
            else:
                readiness = RunReadiness.BLOCKED
                target_status = TaskStatus.BLOCKED
                reason = "运行成功,但 QC 状态与审查/跳过记录不一致,不能安全继续。"
        elif run_state in {WorkflowState.FAILED_RETRYABLE, WorkflowState.TIMED_OUT}:
            readiness = RunReadiness.WAITING_RUN
            target_status = TaskStatus.WAITING_RUN
            waiting_condition = WaitingCondition(
                kind=WaitingKind.WORKFLOW_RUN,
                reference=run_id,
                reason="Worker 正在按现有重试预算处理同一 Workflow Run。",
            )
            reason = (
                f"Workflow 暂处于 {run_state.value};任务继续等待 Worker 对同一 Run 的重试,"
                "不创建新 Run。"
            )
        elif run_state in {
            WorkflowState.FAILED_TERMINAL,
            WorkflowState.CANCELLED,
        }:
            readiness = RunReadiness.BLOCKED
            target_status = TaskStatus.BLOCKED
            reason = f"权威 Workflow 状态为 {run_state.value};本协调器不会创建新 Run 或重试。"

        updated_task = task
        if target_status is not None and target_status is not task.status:
            updated_task = transition_task(
                task,
                target_status,
                waiting_condition=waiting_condition,
            )
            updated_task = self._save_task(
                updated_task,
                expected_version=task.version,
                claim_token=claim_token,
            )
        elif (
            target_status
            in {
                TaskStatus.WAITING_RUN,
                TaskStatus.WAITING_USER,
                TaskStatus.WAITING_DEPENDENCY,
            }
            and task.status is target_status
            and task.waiting_condition != waiting_condition
        ):
            # Same-state changes are persisted through a versioned copy because
            # the pure transition graph intentionally disallows self-transitions.
            updated_task = WorkTask.model_validate(
                {
                    **task.model_dump(),
                    "waiting_condition": waiting_condition,
                    "version": task.version + 1,
                }
            )
            updated_task = self._save_task(
                updated_task,
                expected_version=task.version,
                claim_token=claim_token,
            )

        return WorkflowRunReconciliation(
            task=updated_task,
            readiness=readiness,
            event_receipts=tuple(receipts),
            authoritative=snapshot,
            reason=reason,
        )

    def _reconcile_statistical_run(
        self,
        task: WorkTask,
        run_id: str,
        receipts: tuple[EventReconciliationReceipt, ...],
        *,
        claim_token: str | None = None,
    ) -> WorkflowRunReconciliation:
        if self._statistical_step(task) is None:
            return self._block_statistical_task(
                task,
                run_id,
                receipts,
                reason="统计 WorkTask 必须恰好包含一个 statistical_run 步骤；当前绑定无效。",
                claim_token=claim_token,
            )
        try:
            snapshot, evidence_error = self._read_stable_statistical_snapshot(task, run_id)
        except NotFoundError:
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=receipts,
                reason="绑定的统计 Run 暂不可读取；任务状态未推进。",
            )
        except _InvalidStatisticalAuthority as exc:
            return self._block_statistical_task(
                task,
                run_id,
                receipts,
                reason=str(exc),
                claim_token=claim_token,
            )
        if snapshot is None:
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=receipts,
                reason="统计 Run 在三次权威状态读取期间都发生版本变化；保留原状态后重试。",
            )

        state = WorkflowState(snapshot.run_state)
        if evidence_error is not None:
            return self._block_statistical_task(
                task,
                run_id,
                receipts,
                reason=evidence_error,
                authoritative=snapshot,
                claim_token=claim_token,
            )
        if state in {WorkflowState.QUEUED, WorkflowState.RUNNING, WorkflowState.CANCELLING}:
            return self._apply_statistical_task_state(
                task,
                run_id,
                receipts,
                snapshot,
                target=TaskStatus.WAITING_RUN,
                readiness=RunReadiness.WAITING_RUN,
                waiting_condition=WaitingCondition(
                    kind=WaitingKind.WORKFLOW_RUN,
                    reference=run_id,
                    reason="等待该统计 Workflow Run 进入可核验的终态。",
                ),
                reason="权威统计 Workflow 状态仍在排队、运行或取消中。",
                claim_token=claim_token,
            )
        if state is WorkflowState.SUCCEEDED:
            if snapshot.statistical_result_ids:
                if snapshot.verified_statistical_result_id is None:
                    return self._block_statistical_task(
                        task,
                        run_id,
                        receipts,
                        reason="统计 Run 的结果记录未通过真实科学结果核验；任务已阻断。",
                        authoritative=snapshot,
                        claim_token=claim_token,
                    )
                return self._complete_statistical_task(
                    task,
                    run_id,
                    receipts,
                    snapshot,
                    claim_token=claim_token,
                )
            if "mock.result" in snapshot.registered_artifact_types or any(
                artifact_type.startswith("synthetic.")
                for artifact_type in snapshot.registered_artifact_types
            ):
                return self._block_statistical_task(
                    task,
                    run_id,
                    receipts,
                    reason=(
                        "统计 Run 是 mock/synthetic 执行且没有真实 StatisticalResult；"
                        "不能作为科学分析完成。"
                    ),
                    authoritative=snapshot,
                    claim_token=claim_token,
                )
            return self._apply_statistical_task_state(
                task,
                run_id,
                receipts,
                snapshot,
                target=TaskStatus.WAITING_DEPENDENCY,
                readiness=RunReadiness.WAITING_DEPENDENCY,
                waiting_condition=WaitingCondition(
                    kind=WaitingKind.DEPENDENCY,
                    reference=f"statistical-result:{run_id}",
                    reason="统计 Run 已成功，等待唯一真实 StatisticalResult 完成登记并通过核验。",
                ),
                reason=(
                    "统计 Run 已成功但尚无 StatisticalResult；保留 waiting_dependency "
                    "并等待结果登记。"
                ),
                claim_token=claim_token,
            )
        if state in {WorkflowState.FAILED_RETRYABLE, WorkflowState.TIMED_OUT}:
            if task.status is TaskStatus.WAITING_RUN:
                return self._apply_statistical_task_state(
                    task,
                    run_id,
                    receipts,
                    snapshot,
                    target=TaskStatus.WAITING_RUN,
                    readiness=RunReadiness.WAITING_RUN,
                    waiting_condition=WaitingCondition(
                        kind=WaitingKind.WORKFLOW_RUN,
                        reference=run_id,
                        reason="Worker 正在按原有预算处理同一统计 Run。",
                    ),
                    reason=f"统计 Workflow 暂处于 {state.value}；继续等待同一 Run 的处理。",
                    claim_token=claim_token,
                )
            return self._block_statistical_task(
                task,
                run_id,
                receipts,
                reason="等待统计结果时 Run 又回到非终态，任务与权威 Workflow 状态不一致。",
                authoritative=snapshot,
                claim_token=claim_token,
            )
        return self._block_statistical_task(
            task,
            run_id,
            receipts,
            reason=f"统计 Workflow 已处于 {state.value}；本任务不会自动重试或创建新 Run。",
            authoritative=snapshot,
            claim_token=claim_token,
        )

    def _read_stable_statistical_snapshot(
        self,
        task: WorkTask,
        run_id: str,
        *,
        max_attempts: int = 3,
    ) -> tuple[AuthoritativeRunSnapshot | None, str | None]:
        for _attempt in range(max_attempts):
            run_before = self._run_repository.get_run(run_id)
            run_failure: str | None = None
            if run_before.run_id != run_id:
                run_failure = "统计 Run 读取端口返回了其他 Run；任务已阻断。"

            plan = None
            design: StatisticalDesignRevision | None = None
            project_id = run_before.project_id
            try:
                if run_before.plan_revision_id != task.authority_refs.plan_revision_id:
                    raise _InvalidStatisticalAuthority(
                        "WorkTask 与统计 Run 引用的 Plan 不一致；任务已阻断。"
                    )
                if task.authority_refs.workflow_run_id != run_id:
                    raise _InvalidStatisticalAuthority(
                        "WorkTask 与统计 Run 的绑定 ID 不一致；任务已阻断。"
                    )
                if task.authority_refs.conversation_id is None:
                    raise _InvalidStatisticalAuthority("统计 WorkTask 缺少会话绑定；任务已阻断。")
                conversation = self._conversation_repository.get_conversation(
                    task.authority_refs.conversation_id
                )
                self._require_work_conversation(
                    conversation, conversation.conversation_id, project_id
                )
                if task.authority_refs.plan_revision_id is None:
                    raise _InvalidStatisticalAuthority(
                        "统计 WorkTask 缺少冻结 Plan 引用；任务已阻断。"
                    )
                plan = self._plan_repository.get_plan(task.authority_refs.plan_revision_id)
                step = self._statistical_step(task)
                if step is None or not step.input_version:
                    raise _InvalidStatisticalAuthority(
                        "统计 WorkTask 缺少唯一提案哈希；任务已阻断。"
                    )
                context, design = self._statistical_plan_context(
                    plan,
                    conversation_id=conversation.conversation_id,
                    project_id=project_id,
                    proposal_hash=step.input_version,
                )
                if task.authority_refs.input_revision != context["draft_hash"]:
                    raise _InvalidStatisticalAuthority(
                        "统计 WorkTask 的草稿修订与冻结 Plan work_context 不一致；任务已阻断。"
                    )
                if plan.plan_revision_id != run_before.plan_revision_id:
                    raise _InvalidStatisticalAuthority(
                        "统计 Run 实际 Plan revision 与当前冻结 Plan 不一致；任务已阻断。"
                    )
                if (
                    step is None
                    or step.status is not TaskStepStatus.WAITING
                    or step.authority_refs.plan_revision_id != plan.plan_revision_id
                    or step.authority_refs.workflow_run_id != run_id
                ):
                    raise _InvalidStatisticalAuthority(
                        "统计步骤的 Plan/Run 权威引用不完整或不一致；任务已阻断。"
                    )
                if task.task_id != self.analysis_proposal_task_id(
                    conversation.conversation_id, context["proposal_hash"]
                ):
                    raise _InvalidStatisticalAuthority(
                        "统计 WorkTask ID 与 conversation/proposal 绑定不一致；任务已阻断。"
                    )
            except NotFoundError:
                run_failure = "统计 WorkTask 引用的会话或冻结 Plan 不存在；任务已阻断。"
                plan = None
                design = None
            except (_InvalidStatisticalAuthority, ConflictError, InputValidationError) as exc:
                run_failure = str(exc)
                plan = None
                design = None

            try:
                artifacts = self._artifact_repository.list_artifacts(run_id)
            except NotFoundError:
                artifacts = []
                run_failure = run_failure or "统计 Run 的 Artifact 记录暂不可读取。"
            if any(
                artifact.run_id != run_id or artifact.project_id != project_id
                for artifact in artifacts
            ):
                run_failure = (
                    run_failure or "统计 Artifact 仓储返回了其他项目或 Run 的记录；任务已阻断。"
                )
            artifact_ids = [artifact.artifact_id for artifact in artifacts]
            if len(artifact_ids) != len(set(artifact_ids)):
                run_failure = (
                    run_failure or "统计 Artifact 仓储返回了重复 Artifact ID；任务已阻断。"
                )

            summaries: list[StatisticalResultView] = []
            if run_failure is None and run_before.state is WorkflowState.SUCCEEDED:
                summaries = self._artifact_repository.list_statistical_results(
                    project_id=project_id,
                    run_id=run_id,
                )
                if any(
                    result.project_id != project_id or result.run_id != run_id
                    for result in summaries
                ):
                    run_failure = "统计结果列表包含其他项目或 Run 的记录；任务已阻断。"
                elif len(summaries) > 1:
                    run_failure = "同一统计 Run 注册了多个 StatisticalResult；任务已阻断。"
                elif (
                    summaries
                    and design is not None
                    and summaries[0].design_revision_id != design.revision_id
                ):
                    run_failure = (
                        "StatisticalResult.design_revision_id 与冻结 Plan design 不一致；"
                        "任务已阻断。"
                    )

            verified_id: str | None = None
            result_artifact_ids: tuple[str, ...] = ()
            if summaries and run_failure is None and plan is not None and design is not None:
                try:
                    detail = self._artifact_repository.get_statistical_result(
                        summaries[0].result_id
                    )
                    result_artifact_ids = self._verify_statistical_result(
                        summary=summaries[0],
                        detail=detail,
                        plan=plan,
                        design=design,
                        run_id=run_id,
                        project_id=project_id,
                        registered_artifacts=artifacts,
                    )
                    verified_id = detail.result_id
                except NotFoundError:
                    run_failure = "StatisticalResult 列表项对应的 detail 不存在；任务已阻断。"
                except (ValueError, TypeError, KeyError, AttributeError, ValidationError) as exc:
                    run_failure = (
                        "StatisticalResult 权威凭据无法通过核验；任务已阻断。"
                        f"({type(exc).__name__})"
                    )

            run_after = self._run_repository.get_run(run_id)
            if run_after.run_id != run_id:
                raise _InvalidStatisticalAuthority("统计 Run 读取端口返回了其他 Run；任务已阻断。")
            if run_before.version != run_after.version:
                continue
            snapshot = AuthoritativeRunSnapshot(
                run_id=run_id,
                run_state=run_after.state.value,
                run_version=run_after.version,
                attempt=run_after.attempt,
                qc_mode=run_after.qc_mode.value,
                qc_status=run_after.qc_status,
                registered_artifact_ids=tuple(sorted(item.artifact_id for item in artifacts)),
                registered_artifact_types=tuple(sorted({item.artifact_type for item in artifacts})),
                statistical_result_ids=tuple(result.result_id for result in summaries),
                verified_statistical_result_id=verified_id,
                result_artifact_ids=result_artifact_ids,
            )
            return snapshot, run_failure
        return None, None

    @staticmethod
    def _verify_statistical_result(
        *,
        summary: StatisticalResultView,
        detail: StatisticalResultDetailView,
        plan: Any,
        design: StatisticalDesignRevision,
        run_id: str,
        project_id: str,
        registered_artifacts: list[Any],
    ) -> tuple[str, ...]:
        manifest, report_design, report = verified_result_design(detail)
        report_result = report.get("result")
        frozen_bindings = report.get("frozen_bindings")
        if not isinstance(report_result, Mapping) or not isinstance(frozen_bindings, Mapping):
            raise ValueError("report identities are missing")
        detail_identity = (
            detail.result_id,
            detail.project_id,
            detail.run_id,
            detail.design_revision_id,
            detail.mode,
            detail.non_scientific,
            detail.bundle_hash,
            detail.version,
            detail.artifact_count,
            detail.cluster_count,
        )
        summary_identity = (
            summary.result_id,
            summary.project_id,
            summary.run_id,
            summary.design_revision_id,
            summary.mode,
            summary.non_scientific,
            summary.bundle_hash,
            summary.version,
            summary.artifact_count,
            summary.cluster_count,
        )
        if detail_identity != summary_identity:
            raise ValueError("result list/detail identities disagree")
        if (
            detail.project_id != project_id
            or detail.run_id != run_id
            or detail.design_revision_id != design.revision_id
            or report_design.revision_id != design.revision_id
            or manifest.design_revision_id != design.revision_id
            or manifest.run_id != run_id
            or manifest.result_id != detail.result_id
            or frozen_bindings.get("design_revision_id") != design.revision_id
            or frozen_bindings.get("plan_hash") != plan.plan_hash
            or report_result.get("result_id") != detail.result_id
            or report_result.get("run_id") != run_id
            or report_result.get("mode") != detail.mode
            or report_result.get("non_scientific") != detail.non_scientific
            or report_result.get("non_scientific_reason") != detail.non_scientific_reason
            or detail.artifact_count != len(manifest.artifacts)
            or detail.cluster_count != len(manifest.clusters)
        ):
            raise ValueError("report, manifest, detail, Plan or Run identities disagree")
        if (
            detail.mode != StatisticalResultMode.REAL.value
            or manifest.mode is not StatisticalResultMode.REAL
            or detail.non_scientific
            or manifest.non_scientific
            or any(artifact.placeholder for artifact in manifest.artifacts)
        ):
            raise ValueError("result is synthetic, marked non-scientific, or has placeholders")

        registered_by_id = {item.artifact_id: item for item in registered_artifacts}
        artifact_ids: list[str] = []
        for metadata in manifest.artifacts:
            artifact = registered_by_id.get(metadata.artifact_id)
            if artifact is None:
                raise ValueError("result manifest Artifact is not registered for this Run")
            if (
                artifact.project_id != project_id
                or artifact.run_id != run_id
                or artifact.artifact_type != metadata.artifact_type
                or artifact.relative_path != metadata.relative_path
                or artifact.checksum != metadata.checksum_sha256
                or artifact.size_bytes != metadata.size_bytes
                or content_hash(artifact.provenance) != metadata.provenance_hash
            ):
                raise ValueError(
                    "result manifest Artifact metadata differs from registered Artifact"
                )
            artifact_ids.append(metadata.artifact_id)
        if len(set(artifact_ids)) != len(artifact_ids) or not artifact_ids:
            raise ValueError("verified statistical result Artifact IDs are empty or duplicated")
        return tuple(sorted(artifact_ids))

    def _apply_statistical_task_state(
        self,
        task: WorkTask,
        run_id: str,
        receipts: tuple[EventReconciliationReceipt, ...],
        snapshot: AuthoritativeRunSnapshot,
        *,
        target: TaskStatus,
        readiness: RunReadiness,
        waiting_condition: WaitingCondition,
        reason: str,
        claim_token: str | None = None,
    ) -> WorkflowRunReconciliation:
        updated = task
        if task.status is not target or task.waiting_condition != waiting_condition:
            if task.status is TaskStatus.WAITING_DEPENDENCY and target is TaskStatus.WAITING_RUN:
                return self._block_statistical_task(
                    task,
                    run_id,
                    receipts,
                    reason="统计任务等待结果时 Run 又回到非终态，任务与 Workflow 状态不一致。",
                    authoritative=snapshot,
                    claim_token=claim_token,
                )
            if task.status is not target:
                updated = transition_task(task, target, waiting_condition=waiting_condition)
            else:
                updated = WorkTask.model_validate(
                    {
                        **task.model_dump(),
                        "waiting_condition": waiting_condition.model_dump(),
                        "version": task.version + 1,
                    }
                )
            updated = self._save_task(
                updated,
                expected_version=task.version,
                claim_token=claim_token,
            )
        return WorkflowRunReconciliation(
            task=updated,
            readiness=readiness,
            event_receipts=receipts,
            authoritative=snapshot,
            reason=reason,
        )

    def _complete_statistical_task(
        self,
        task: WorkTask,
        run_id: str,
        receipts: tuple[EventReconciliationReceipt, ...],
        snapshot: AuthoritativeRunSnapshot,
        *,
        claim_token: str | None = None,
    ) -> WorkflowRunReconciliation:
        step = self._statistical_step(task)
        if (
            step is None
            or step.status is not TaskStepStatus.WAITING
            or snapshot.verified_statistical_result_id is None
            or not snapshot.result_artifact_ids
        ):
            return self._block_statistical_task(
                task,
                run_id,
                receipts,
                reason="统计结果虽存在，但 WorkTask 步骤或 Artifact 完成绑定无效；任务已阻断。",
                authoritative=snapshot,
                claim_token=claim_token,
            )
        succeeded_step = transition_task_step(step, TaskStepStatus.SUCCEEDED)
        result_artifact_ids = tuple(sorted(set(snapshot.result_artifact_ids)))
        succeeded_step = succeeded_step.model_copy(
            update={
                "authority_refs": succeeded_step.authority_refs.model_copy(
                    update={"artifact_ids": result_artifact_ids}
                )
            }
        )
        completed = transition_task(task, TaskStatus.COMPLETED).model_copy(
            update={
                "authority_refs": task.authority_refs.model_copy(
                    update={"artifact_ids": result_artifact_ids}
                ),
                "steps": tuple(
                    succeeded_step if item.step_id == step.step_id else item for item in task.steps
                ),
            }
        )
        completed = self._save_task(
            WorkTask.model_validate(completed.model_dump()),
            expected_version=task.version,
            claim_token=claim_token,
        )
        return WorkflowRunReconciliation(
            task=completed,
            readiness=RunReadiness.COMPLETED,
            event_receipts=receipts,
            authoritative=snapshot,
            reason="唯一真实统计结果及其 Artifact 已与冻结设计和 Plan 绑定并通过核验；任务已完成。",
        )

    def _block_statistical_task(
        self,
        task: WorkTask,
        run_id: str,
        receipts: tuple[EventReconciliationReceipt, ...],
        *,
        reason: str,
        authoritative: AuthoritativeRunSnapshot | None = None,
        claim_token: str | None = None,
    ) -> WorkflowRunReconciliation:
        updated = task
        if task.status in {TaskStatus.WAITING_RUN, TaskStatus.WAITING_DEPENDENCY}:
            updates: dict[str, Any] = {}
            step = self._statistical_step(task)
            if step is not None and step.status is TaskStepStatus.WAITING:
                blocked_step = transition_task_step(step, TaskStepStatus.BLOCKED)
                updates["steps"] = tuple(
                    blocked_step if item.step_id == step.step_id else item for item in task.steps
                )
            updated = transition_task(task, TaskStatus.BLOCKED).model_copy(update=updates)
            updated = self._save_task(
                WorkTask.model_validate(updated.model_dump()),
                expected_version=task.version,
                claim_token=claim_token,
            )
        return WorkflowRunReconciliation(
            task=updated,
            readiness=RunReadiness.BLOCKED,
            event_receipts=receipts,
            authoritative=authoritative,
            reason=reason,
        )

    @staticmethod
    def _has_statistical_step(task: WorkTask) -> bool:
        return any(step.kind == "statistical_run" for step in task.steps)

    def _save_task(
        self,
        task: WorkTask,
        *,
        expected_version: int,
        claim_token: str | None,
    ) -> WorkTask:
        if claim_token is None:
            return self._repository.save_task(task, expected_version=expected_version)
        return self._repository.save_task(
            task,
            expected_version=expected_version,
            claim_token=claim_token,
        )

    def _read_stable_authoritative_snapshot(
        self,
        task: WorkTask,
        run_id: str,
        *,
        max_attempts: int = 3,
    ) -> AuthoritativeRunSnapshot | None:
        """Read dependent authority records bracketed by a stable Run version."""

        for _attempt in range(max_attempts):
            run_before = self._run_repository.get_run(run_id)
            if run_before.run_id != run_id:
                raise InputValidationError(
                    "authoritative_run_mismatch",
                    "运行读取端口返回了与任务绑定 ID 不一致的记录。",
                    expected_run_id=run_id,
                    actual_run_id=run_before.run_id,
                )

            review = self._qc_repository.get_latest_qc_review_for_run(run_id)
            skip = self._qc_repository.get_qc_skip_for_run(run_id)
            artifacts = self._artifact_repository.list_artifacts(run_id)
            if review is not None and review.run_id != run_id:
                raise InputValidationError(
                    "authoritative_qc_mismatch",
                    "QC 读取端口返回了其他运行的审查记录。",
                    expected_run_id=run_id,
                    actual_run_id=review.run_id,
                )
            if skip is not None and skip.run_id != run_id:
                raise InputValidationError(
                    "authoritative_qc_skip_mismatch",
                    "QC 读取端口返回了其他运行的跳过记录。",
                    expected_run_id=run_id,
                    actual_run_id=skip.run_id,
                )
            if any(artifact.run_id != run_id for artifact in artifacts):
                raise InputValidationError(
                    "authoritative_artifact_mismatch",
                    "Artifact 读取端口返回了其他运行的产物。",
                    expected_run_id=run_id,
                )

            resolved_references: list[str] = []
            unresolved_references: list[str] = []
            for artifact_id in sorted(set(task.authority_refs.artifact_ids)):
                try:
                    referenced_artifact = self._artifact_repository.get_artifact(artifact_id)
                except NotFoundError:
                    unresolved_references.append(artifact_id)
                    continue
                if (
                    referenced_artifact.artifact_id != artifact_id
                    or referenced_artifact.project_id != run_before.project_id
                ):
                    unresolved_references.append(artifact_id)
                else:
                    resolved_references.append(artifact_id)

            run_after = self._run_repository.get_run(run_id)
            if run_after.run_id != run_id:
                raise InputValidationError(
                    "authoritative_run_mismatch",
                    "运行读取端口返回了与任务绑定 ID 不一致的记录。",
                    expected_run_id=run_id,
                    actual_run_id=run_after.run_id,
                )
            if run_before.version != run_after.version:
                continue

            registered_ids = {artifact.artifact_id for artifact in artifacts}
            return AuthoritativeRunSnapshot(
                run_id=run_after.run_id,
                run_state=run_after.state.value,
                run_version=run_after.version,
                attempt=run_after.attempt,
                qc_mode=run_after.qc_mode.value,
                qc_status=run_after.qc_status,
                latest_qc_review_id=(
                    review.review.review_revision_id if review is not None else None
                ),
                latest_qc_review_state=review.state.value if review is not None else None,
                qc_skip_id=skip.qc_skip_id if skip is not None else None,
                registered_artifact_ids=tuple(sorted(registered_ids)),
                registered_artifact_types=tuple(sorted({item.artifact_type for item in artifacts})),
                resolved_referenced_artifact_ids=tuple(resolved_references),
                unresolved_referenced_artifact_ids=tuple(unresolved_references),
            )
        return None
