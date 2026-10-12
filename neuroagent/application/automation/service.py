"""Narrow application use cases for durable user-goal progress."""

from __future__ import annotations

from neuroagent.application.automation.models import (
    AuthoritativeRunSnapshot,
    EventConsumptionDecision,
    EventReconciliationReceipt,
    RunReadiness,
    TaskStatus,
    WaitingCondition,
    WaitingKind,
    WorkEventIdentity,
    WorkflowRunReconciliation,
    WorkTask,
    transition_task,
)
from neuroagent.application.automation.ports import WorkTaskRepositoryPort
from neuroagent.application.errors import (
    ConflictError,
    InputValidationError,
    NotFoundError,
)
from neuroagent.application.repository_ports import (
    ArtifactCatalogRepositoryPort,
    EventRepositoryPort,
    QcRepositoryPort,
    RunRepositoryPort,
)
from neuroagent.domain.lifecycle import WorkflowState


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
    ) -> None:
        self._repository = repository
        self._event_repository = event_repository
        self._run_repository = run_repository
        self._qc_repository = qc_repository
        self._artifact_repository = artifact_repository

    def create_task(self, task: WorkTask) -> WorkTask:
        return self._repository.create_task(task)

    def get_task(self, task_id: str) -> WorkTask:
        return self._repository.get_task(task_id)

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
        )
        # Read after both applied and no-op decisions so a concurrent duplicate
        # returns current persisted state rather than a stale pre-read snapshot.
        return decision, self._repository.get_task(task_id)

    def reconcile_waiting_run(self, task_id: str) -> WorkflowRunReconciliation:
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
        is_waiting_for_bound_run = (
            task.status is TaskStatus.WAITING_RUN
            and task.waiting_condition is not None
            and task.waiting_condition.reference == run_id
        )
        is_waiting_for_bound_qc = (
            task.status is TaskStatus.WAITING_USER
            and task.waiting_condition is not None
            and task.waiting_condition.reference == f"qc:{run_id}"
        )
        if not (is_waiting_for_bound_run or is_waiting_for_bound_qc):
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
        events = self._event_repository.list_events(run_id=run_id, after_event_id=cursor)
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
            decision, task = self.consume_workflow_event(task_id, identity)
            receipts.append(EventReconciliationReceipt(event_id=event.event_id, decision=decision))

        # Consume can race with user/API state changes. Always use the persisted
        # task version before deciding whether this reconciliation may transition it.
        task = self._repository.get_task(task_id)
        if (
            task.status is TaskStatus.WAITING_RUN
            and task.waiting_condition is not None
            and task.waiting_condition.reference == run_id
        ):
            pass
        elif not (
            task.status is TaskStatus.WAITING_USER
            and task.waiting_condition is not None
            and task.waiting_condition.reference == f"qc:{run_id}"
        ):
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=tuple(receipts),
                reason="任务等待状态已并发变化;本次只记录事件身份,需重新读取后再协调。",
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
            updated_task = self._repository.save_task(
                updated_task,
                expected_version=task.version,
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
            updated_task = self._repository.save_task(
                updated_task,
                expected_version=task.version,
            )

        return WorkflowRunReconciliation(
            task=updated_task,
            readiness=readiness,
            event_receipts=tuple(receipts),
            authoritative=snapshot,
            reason=reason,
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
