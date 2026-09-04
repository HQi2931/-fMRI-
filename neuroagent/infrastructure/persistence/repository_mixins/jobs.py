"""Atomic job claiming, leasing, and finalization."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, or_, select, update
from sqlalchemy.orm import Session

from neuroagent.application.contracts import WorkflowState
from neuroagent.application.errors import ConflictError, NotFoundError
from neuroagent.application.hashing import canonical_json
from neuroagent.application.ports import ClaimedJob
from neuroagent.infrastructure.persistence.models import (
    JobRow,
    PlanRevisionRow,
    RuntimeEventRow,
    WorkflowRunRow,
)
from neuroagent.infrastructure.persistence.repository_mixins._base import (
    RepositoryBaseMixin,
    _as_utc,
    _load,
)
from neuroagent.infrastructure.persistence.statistical_completion import (
    register_real_statistical_result,
)
from neuroagent.observability.events import redact_event_payload
from neuroagent.observability.tracing import current_trace_id
from neuroagent.workflow.state import validate_workflow_transition


class JobExecutionMixin(RepositoryBaseMixin):
    def claim_next_job(self, worker_id: str, lease_seconds: int) -> ClaimedJob | None:
        now = datetime.now(UTC)
        with self._write_session() as session:
            expired = session.scalars(
                select(JobRow).where(
                    JobRow.state == "running",
                    JobRow.lease_expires_at.is_not(None),
                    JobRow.lease_expires_at < now,
                )
            ).all()
            for exhausted in expired:
                run = session.get(WorkflowRunRow, exhausted.run_id)
                if run is None:
                    exhausted.state = "failed_terminal"
                    exhausted.error = "worker lease expired after retry budget was exhausted"
                    exhausted.lease_owner = None
                    exhausted.lease_expires_at = None
                    exhausted.updated_at = now
                    continue
                current = WorkflowState(run.state)
                if not (
                    exhausted.attempt >= exhausted.max_attempts
                    or run.cancel_requested
                    or current is WorkflowState.CANCELLING
                ):
                    continue
                target = (
                    WorkflowState.CANCELLED
                    if run.cancel_requested or current is WorkflowState.CANCELLING
                    else WorkflowState.FAILED_TERMINAL
                )
                if current is not target:
                    if current is WorkflowState.RUNNING and target is WorkflowState.CANCELLED:
                        validate_workflow_transition(current, WorkflowState.CANCELLING)
                        validate_workflow_transition(
                            WorkflowState.CANCELLING, WorkflowState.CANCELLED
                        )
                        run.version += 1
                    else:
                        validate_workflow_transition(current, target)
                    run.state = target.value
                    run.version += 1
                run.error = (
                    None
                    if target is WorkflowState.CANCELLED
                    else "worker lease expired after retry budget was exhausted"
                )
                run.updated_at = now
                exhausted.state = (
                    "cancelled" if target is WorkflowState.CANCELLED else "failed_terminal"
                )
                exhausted.error = run.error
                exhausted.lease_owner = None
                exhausted.lease_expires_at = None
                exhausted.updated_at = now
                session.add(
                    RuntimeEventRow(
                        trace_id=current_trace_id(),
                        project_id=run.project_id,
                        run_id=run.run_id,
                        event_type=(
                            "JobLeaseCancellationRecovered"
                            if target is WorkflowState.CANCELLED
                            else "JobRetryBudgetExhausted"
                        ),
                        severity=("warning" if target is WorkflowState.CANCELLED else "error"),
                        payload_json=canonical_json(
                            {
                                "job_id": exhausted.job_id,
                                "attempt": exhausted.attempt,
                                "max_attempts": exhausted.max_attempts,
                                "to_state": target.value,
                            }
                        ),
                    )
                )
            candidate = session.scalar(
                select(JobRow)
                .where(
                    JobRow.attempt < JobRow.max_attempts,
                    or_(
                        JobRow.state == "queued",
                        (JobRow.state == "running")
                        & (JobRow.lease_expires_at.is_not(None))
                        & (JobRow.lease_expires_at < now),
                    ),
                )
                .order_by(JobRow.created_at, JobRow.job_id)
                .limit(1)
            )
            if candidate is None:
                return None
            previous_job_state = candidate.state
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(JobRow)
                    .where(
                        JobRow.job_id == candidate.job_id,
                        JobRow.attempt < JobRow.max_attempts,
                        or_(
                            JobRow.state == "queued",
                            (JobRow.state == "running")
                            & (JobRow.lease_expires_at.is_not(None))
                            & (JobRow.lease_expires_at < now),
                        ),
                    )
                    .execution_options(synchronize_session=False)
                    .values(
                        state="running",
                        lease_owner=worker_id,
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        attempt=JobRow.attempt + 1,
                        updated_at=now,
                    )
                ),
            )
            if result.rowcount != 1:
                return None
            session.refresh(candidate)
            run = session.get(WorkflowRunRow, candidate.run_id)
            if run is None:
                raise NotFoundError("run", candidate.run_id)
            previous_run_state = WorkflowState(run.state)
            if previous_run_state in {
                WorkflowState.FAILED_RETRYABLE,
                WorkflowState.TIMED_OUT,
            }:
                validate_workflow_transition(previous_run_state, WorkflowState.QUEUED)
                validate_workflow_transition(WorkflowState.QUEUED, WorkflowState.RUNNING)
                run.version += 2
                run.state = WorkflowState.RUNNING.value
            elif previous_run_state is WorkflowState.QUEUED:
                validate_workflow_transition(previous_run_state, WorkflowState.RUNNING)
                run.version += 1
                run.state = WorkflowState.RUNNING.value
            elif previous_run_state is not WorkflowState.RUNNING:
                raise ConflictError(
                    "job_run_state_mismatch",
                    "可领取 Job 的运行状态与任务状态不一致。",
                    job_state=previous_job_state,
                    run_state=previous_run_state.value,
                )
            elif previous_job_state != "running":
                raise ConflictError(
                    "job_run_state_mismatch",
                    "排队 Job 不能绑定到已经运行的 Workflow。",
                    job_state=previous_job_state,
                    run_state=previous_run_state.value,
                )
            else:
                # Reclaiming an expired lease mutates the observable attempt
                # and lease owner even though the workflow remains RUNNING.
                # Advance the optimistic-concurrency version for that change.
                run.version += 1
            run.attempt += 1
            run.error = None
            run.updated_at = now
            session.add(
                RuntimeEventRow(
                    trace_id=current_trace_id(),
                    project_id=run.project_id,
                    run_id=run.run_id,
                    event_type=(
                        "JobLeaseReclaimed"
                        if previous_job_state == "running"
                        else "WorkflowTransitioned"
                    ),
                    severity="warning" if previous_job_state == "running" else "info",
                    payload_json=canonical_json(
                        {
                            "job_id": candidate.job_id,
                            "worker_id": worker_id,
                            "from_state": previous_run_state.value,
                            "to_state": WorkflowState.RUNNING.value,
                            "attempt": candidate.attempt,
                            "max_attempts": candidate.max_attempts,
                        }
                    ),
                )
            )
            return {
                "job_id": candidate.job_id,
                "run_id": candidate.run_id,
                "payload": _load(candidate.payload_json, {}),
                "attempt": candidate.attempt,
                "max_attempts": candidate.max_attempts,
            }

    def is_cancel_requested(self, run_id: str) -> bool:
        with self.database.session_factory() as session:
            row = session.get(WorkflowRunRow, run_id)
            return bool(row and row.cancel_requested)

    def renew_job_lease(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        now = datetime.now(UTC)
        with self._write_session() as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(JobRow)
                    .where(
                        JobRow.job_id == job_id,
                        JobRow.state == "running",
                        JobRow.lease_owner == worker_id,
                        JobRow.lease_expires_at.is_not(None),
                        JobRow.lease_expires_at > now,
                    )
                    .values(
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        updated_at=now,
                    )
                ),
            )
            return result.rowcount == 1

    @staticmethod
    def _owned_active_job(
        session: Session, job_id: str, worker_id: str, now: datetime
    ) -> JobRow | None:
        job = session.get(JobRow, job_id)
        if (
            job is None
            or job.state != "running"
            or job.lease_owner != worker_id
            or job.lease_expires_at is None
            or _as_utc(job.lease_expires_at) <= now
        ):
            return None
        return job

    @staticmethod
    def _add_run_event(
        session: Session,
        *,
        run: WorkflowRunRow,
        event_type: str,
        severity: str,
        payload: dict[str, object],
    ) -> None:
        session.add(
            RuntimeEventRow(
                trace_id=current_trace_id(),
                project_id=run.project_id,
                run_id=run.run_id,
                event_type=event_type,
                severity=severity,
                payload_json=canonical_json(redact_event_payload(payload)),
            )
        )

    def finalize_job_success(
        self,
        job_id: str,
        worker_id: str,
        *,
        result: dict[str, object],
        artifacts: tuple[dict[str, Any], ...],
        actor: str,
    ) -> bool:
        """Atomically register artifacts and move both Job and Run to their success gates."""

        now = datetime.now(UTC)
        with self._write_session() as session:
            job = self._owned_active_job(session, job_id, worker_id, now)
            if job is None:
                return False
            run = session.get(WorkflowRunRow, job.run_id)
            if run is None:
                raise NotFoundError("run", job.run_id)
            if run.cancel_requested or WorkflowState(run.state) is WorkflowState.CANCELLING:
                return self._finalize_cancel_in_session(
                    session,
                    job=job,
                    run=run,
                    now=now,
                    error="cancellation won race with executor completion",
                    actor=actor,
                )
            current = WorkflowState(run.state)
            payload = _load(job.payload_json, {})
            is_statistics_mock = (
                isinstance(payload, dict) and payload.get("run_kind") == "statistics_mock"
            )
            is_statistics_real = (
                isinstance(payload, dict) and payload.get("run_kind") == "statistics_matlab"
            )
            target = (
                WorkflowState.SUCCEEDED
                if is_statistics_mock or is_statistics_real
                else WorkflowState.QC_REVIEW
            )
            validate_workflow_transition(current, target)
            registered_rows = self._register_artifacts_in_session(
                session,
                project_id=run.project_id,
                run=run,
                artifacts=artifacts,
            )
            if is_statistics_real:
                plan = session.get(PlanRevisionRow, run.plan_revision_id)
                if plan is None:
                    raise NotFoundError("plan_revision", run.plan_revision_id)
                register_real_statistical_result(
                    session,
                    run=run,
                    plan=plan,
                    payload=cast(dict[str, Any], payload),
                    artifact_rows=registered_rows,
                    actor=actor,
                    created_at=now,
                )
            job.state = "succeeded"
            job.result_json = canonical_json(result)
            job.error = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            run.state = target.value
            run.version += 1
            run.error = None
            run.updated_at = now
            self._add_run_event(
                session,
                run=run,
                event_type="WorkflowTransitioned",
                severity="info",
                payload={
                    "actor": actor,
                    "job_id": job.job_id,
                    "from_state": current.value,
                    "to_state": target.value,
                    "artifact_count": len(artifacts),
                    "reason": (
                        "synthetic statistics mock completed"
                        if is_statistics_mock
                        else "real statistics evidence registered"
                        if is_statistics_real
                        else "executor completed; manual QC required"
                    ),
                    "synthetic": is_statistics_mock,
                    "execution_backend": payload.get("execution_backend"),
                    "executor_type": payload.get("executor_type"),
                    "plan_hash": payload.get("plan_hash"),
                    "environment_hash": payload.get("environment_hash"),
                    "approval_record_id": payload.get("approval_record_id"),
                    "attempt": job.attempt,
                },
            )
            session.flush()
            return True

    def finalize_job_failure(
        self,
        job_id: str,
        worker_id: str,
        *,
        requested_state: WorkflowState,
        error: str,
        actor: str,
    ) -> bool:
        """Atomically requeue or terminalize a failed job with its Workflow state."""

        if requested_state not in {
            WorkflowState.FAILED_RETRYABLE,
            WorkflowState.TIMED_OUT,
            WorkflowState.FAILED_TERMINAL,
        }:
            raise ValueError("requested_state must be a failure state")
        now = datetime.now(UTC)
        with self._write_session() as session:
            job = self._owned_active_job(session, job_id, worker_id, now)
            if job is None:
                return False
            run = session.get(WorkflowRunRow, job.run_id)
            if run is None:
                raise NotFoundError("run", job.run_id)
            if run.cancel_requested or WorkflowState(run.state) is WorkflowState.CANCELLING:
                return self._finalize_cancel_in_session(
                    session,
                    job=job,
                    run=run,
                    now=now,
                    error=error,
                    actor=actor,
                )
            current = WorkflowState(run.state)
            can_retry = (
                requested_state in {WorkflowState.FAILED_RETRYABLE, WorkflowState.TIMED_OUT}
                and job.attempt < job.max_attempts
            )
            target = requested_state if can_retry else WorkflowState.FAILED_TERMINAL
            validate_workflow_transition(current, target)
            job.state = "queued" if can_retry else "failed_terminal"
            job.result_json = None
            job.error = error
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            run.state = target.value
            run.version += 1
            run.error = error
            run.updated_at = now
            self._add_run_event(
                session,
                run=run,
                event_type="WorkflowTransitioned",
                severity="warning" if can_retry else "error",
                payload={
                    "actor": actor,
                    "job_id": job.job_id,
                    "from_state": current.value,
                    "to_state": target.value,
                    "attempt": job.attempt,
                    "max_attempts": job.max_attempts,
                    "reason": "executor did not complete",
                    "error": error,
                },
            )
            session.flush()
            return True

    def finalize_job_cancel(
        self,
        job_id: str,
        worker_id: str,
        *,
        error: str | None,
        actor: str,
    ) -> bool:
        """Atomically acknowledge cancellation for both the Job and Workflow."""

        now = datetime.now(UTC)
        with self._write_session() as session:
            job = self._owned_active_job(session, job_id, worker_id, now)
            if job is None:
                return False
            run = session.get(WorkflowRunRow, job.run_id)
            if run is None:
                raise NotFoundError("run", job.run_id)
            return self._finalize_cancel_in_session(
                session,
                job=job,
                run=run,
                now=now,
                error=error,
                actor=actor,
            )

    def _finalize_cancel_in_session(
        self,
        session: Session,
        *,
        job: JobRow,
        run: WorkflowRunRow,
        now: datetime,
        error: str | None,
        actor: str,
    ) -> bool:
        current = WorkflowState(run.state)
        if current is WorkflowState.RUNNING:
            validate_workflow_transition(current, WorkflowState.CANCELLING)
            run.state = WorkflowState.CANCELLING.value
            run.version += 1
            current = WorkflowState.CANCELLING
        validate_workflow_transition(current, WorkflowState.CANCELLED)
        run.state = WorkflowState.CANCELLED.value
        run.version += 1
        run.cancel_requested = True
        run.error = None
        run.updated_at = now
        job.state = "cancelled"
        job.result_json = None
        job.error = error
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = now
        self._add_run_event(
            session,
            run=run,
            event_type="WorkflowTransitioned",
            severity="warning",
            payload={
                "actor": actor,
                "job_id": job.job_id,
                "from_state": current.value,
                "to_state": WorkflowState.CANCELLED.value,
                "reason": "executor cancellation finalized",
                "error": error,
            },
        )
        session.flush()
        return True

    def expire_job_lease_for_test(self, run_id: str) -> None:
        """Test seam used to model a worker crash without sleeping."""
        with self._write_session() as session:
            row = session.scalar(select(JobRow).where(JobRow.run_id == run_id))
            if row is None:
                raise NotFoundError("job_for_run", run_id)
            row.state = "running"
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
