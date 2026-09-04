"""Workflow runs and cancellation requests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update

from neuroagent.application.contracts import ApprovalDecision, PlanState, RunView, WorkflowState
from neuroagent.application.errors import ConflictError, NotFoundError
from neuroagent.application.hashing import canonical_json
from neuroagent.infrastructure.persistence.models import (
    ApprovalRow,
    JobRow,
    PlanRevisionRow,
    ProjectRow,
    WorkflowRunRow,
)
from neuroagent.infrastructure.persistence.repository_mixins._base import (
    RepositoryBaseMixin,
    _id,
)
from neuroagent.workflow.state import validate_workflow_transition

_EXECUTOR_TYPES = {
    "workflow_mock",
    "matlab_preprocessing",
    "matlab_statistics",
}


class RunMixin(RepositoryBaseMixin):
    # -- workflow/jobs -------------------------------------------------------

    def create_run(
        self,
        *,
        project_id: str,
        plan_revision_id: str,
        expected_plan_hash: str,
        max_attempts: int,
        payload: dict[str, Any],
    ) -> RunView:
        with self._write_session() as session:
            plan = session.get(PlanRevisionRow, plan_revision_id)
            if plan is None:
                raise NotFoundError("plan_revision", plan_revision_id)
            if plan.plan_hash != expected_plan_hash:
                raise ConflictError(
                    "run_plan_hash_mismatch",
                    "运行请求的计划哈希与当前不可变计划不一致。",
                    expected=plan.plan_hash,
                    received=expected_plan_hash,
                )
            if plan.project_id != project_id:
                raise ConflictError("cross_project_plan", "计划不属于指定项目。")
            if PlanState(plan.state) is not PlanState.APPROVED:
                raise ConflictError(
                    "plan_not_approved", "只能从已批准且未失效的计划创建运行。", state=plan.state
                )
            valid = session.scalar(
                select(func.count(ApprovalRow.approval_id)).where(
                    ApprovalRow.plan_revision_id == plan_revision_id,
                    ApprovalRow.plan_hash == plan.plan_hash,
                    ApprovalRow.decision == ApprovalDecision.APPROVED.value,
                )
            )
            if not valid:
                raise ConflictError("approval_missing", "未找到与计划哈希匹配的有效审批记录。")
            executor_type = str(payload.get("executor_type", "workflow_mock"))
            if executor_type not in _EXECUTOR_TYPES:
                raise ConflictError(
                    "unsupported_executor_type",
                    "运行只能使用已注册的执行器类型。",
                    executor_type=executor_type,
                )
            run = WorkflowRunRow(
                run_id=_id(),
                project_id=project_id,
                plan_revision_id=plan_revision_id,
                state=WorkflowState.QUEUED.value,
            )
            session.add(run)
            session.flush()
            session.add(
                JobRow(
                    job_id=_id(),
                    run_id=run.run_id,
                    executor_type=executor_type,
                    state="queued",
                    payload_json=canonical_json(payload),
                    max_attempts=max_attempts,
                )
            )
            session.flush()
            return self._run(run)

    def get_run(self, run_id: str) -> RunView:
        with self.database.session_factory() as session:
            row = session.get(WorkflowRunRow, run_id)
            if row is None:
                raise NotFoundError("run", run_id)
            return self._run(row)

    def list_runs(
        self,
        *,
        project_id: str | None = None,
        state: WorkflowState | None = None,
    ) -> list[RunView]:
        with self.database.session_factory() as session:
            query = select(WorkflowRunRow)
            if project_id is not None:
                if session.get(ProjectRow, project_id) is None:
                    raise NotFoundError("project", project_id)
                query = query.where(WorkflowRunRow.project_id == project_id)
            if state is not None:
                query = query.where(WorkflowRunRow.state == state.value)
            rows = session.scalars(
                query.order_by(WorkflowRunRow.created_at.desc(), WorkflowRunRow.run_id)
            ).all()
            return [self._run(row) for row in rows]

    def transition_run(
        self,
        run_id: str,
        *,
        expected_version: int,
        target: WorkflowState,
        error: str | None = None,
        increment_attempt: bool = False,
    ) -> RunView:
        now = datetime.now(UTC)
        with self._write_session() as session:
            values: dict[str, Any] = {
                "state": target.value,
                "version": expected_version + 1,
                "updated_at": now,
                "error": error,
            }
            if increment_attempt:
                values["attempt"] = WorkflowRunRow.attempt + 1
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(WorkflowRunRow)
                    .where(
                        WorkflowRunRow.run_id == run_id,
                        WorkflowRunRow.version == expected_version,
                    )
                    .values(**values)
                ),
            )
            if result.rowcount != 1:
                exists = session.get(WorkflowRunRow, run_id)
                if exists is None:
                    raise NotFoundError("run", run_id)
                raise ConflictError(
                    "revision_conflict",
                    "运行版本已变化, 请刷新后重试。",
                    expected=expected_version,
                    actual=exists.version,
                )
            row = session.get(WorkflowRunRow, run_id)
            assert row is not None
            return self._run(row)

    def request_cancel(self, run_id: str, expected_version: int) -> RunView:
        with self._write_session() as session:
            run = session.get(WorkflowRunRow, run_id)
            if run is None:
                raise NotFoundError("run", run_id)
            self._check_version("run", run.version, expected_version)
            state = WorkflowState(run.state)
            if state is WorkflowState.QUEUED:
                validate_workflow_transition(state, WorkflowState.CANCELLED)
                run.state = WorkflowState.CANCELLED.value
                job = session.scalar(select(JobRow).where(JobRow.run_id == run_id))
                if job:
                    job.state = "cancelled"
            elif state is WorkflowState.RUNNING:
                validate_workflow_transition(state, WorkflowState.CANCELLING)
                run.state = WorkflowState.CANCELLING.value
            elif state in {WorkflowState.FAILED_RETRYABLE, WorkflowState.TIMED_OUT}:
                validate_workflow_transition(state, WorkflowState.CANCELLED)
                run.state = WorkflowState.CANCELLED.value
                job = session.scalar(select(JobRow).where(JobRow.run_id == run_id))
                if job:
                    job.state = "cancelled"
            elif state is WorkflowState.CANCELLING:
                return self._run(run)
            else:
                raise ConflictError(
                    "run_not_cancellable", "当前运行状态不能取消。", state=state.value
                )
            run.cancel_requested = True
            run.version += 1
            run.updated_at = datetime.now(UTC)
            session.flush()
            return self._run(run)
