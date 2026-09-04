"""SQLite-backed worker that executes only registered executor objects.

This module contains no subprocess entry point.  The initial worker uses the
MockJobExecutor; a later MATLAB adapter can implement the same JobExecutor
port without changing API or workflow state rules.
"""

from __future__ import annotations

import socket
import threading
import uuid
from typing import Any, Protocol

from neuroagent.application.contracts import PlanRevisionView, RunView, WorkflowState
from neuroagent.application.ports import ClaimedJob, JobExecutor


class WorkerRepository(Protocol):
    """Document the repository surface used by SQLiteWorker."""

    def claim_next_job(self, worker_id: str, lease_seconds: int) -> ClaimedJob | None: ...

    def is_cancel_requested(self, run_id: str) -> bool: ...

    def renew_job_lease(self, job_id: str, worker_id: str, lease_seconds: int) -> bool: ...

    def finalize_job_success(
        self,
        job_id: str,
        worker_id: str,
        *,
        result: dict[str, object],
        artifacts: tuple[dict[str, Any], ...],
        actor: str,
    ) -> bool: ...

    def finalize_job_failure(
        self,
        job_id: str,
        worker_id: str,
        *,
        requested_state: WorkflowState,
        error: str,
        actor: str,
    ) -> bool: ...

    def finalize_job_cancel(
        self,
        job_id: str,
        worker_id: str,
        *,
        error: str | None,
        actor: str,
    ) -> bool: ...

    def get_run(self, run_id: str) -> RunView: ...

    def get_plan(self, plan_revision_id: str) -> PlanRevisionView: ...

    def append_event(
        self,
        *,
        project_id: str | None,
        run_id: str | None,
        event_type: str,
        severity: str,
        payload: dict[str, object],
    ) -> None: ...


class SQLiteWorker:
    def __init__(
        self,
        repository: WorkerRepository,
        executor: JobExecutor,
        *,
        worker_id: str | None = None,
        lease_seconds: int = 30,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be at least 1")
        self._repository = repository
        self._executor = executor
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self._lease_seconds = lease_seconds

    def run_once(self) -> bool:
        claimed = self._repository.claim_next_job(self.worker_id, self._lease_seconds)
        if claimed is None:
            return False

        job_id = str(claimed["job_id"])
        run_id = str(claimed["run_id"])
        run = self._repository.get_run(run_id)
        self._repository.append_event(
            project_id=run.project_id,
            run_id=run_id,
            event_type="RunStageStarted",
            severity="info",
            payload={
                "stage": "staging",
                "stage_progress": 0.0,
                "attempt": int(claimed.get("attempt", 1)),
                "log_cursor": 0,
            },
        )

        if self._repository.is_cancel_requested(run_id):
            if not self._repository.finalize_job_cancel(
                job_id,
                self.worker_id,
                error=None,
                actor=self.worker_id,
            ):
                self._record_lease_lost(run.project_id, run_id, job_id)
            return True

        payload = claimed.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        plan = self._repository.get_plan(run.plan_revision_id)
        payload = {
            **payload,
            "job_id": job_id,
            "run_id": run_id,
            "plan_hash": str(payload.get("plan_hash", plan.plan_hash)),
            "input_manifest_hash": str(payload.get("input_manifest_hash", plan.manifest_hash)),
        }
        lease_lost = threading.Event()
        heartbeat_stop = threading.Event()
        heartbeat_interval = max(0.05, self._lease_seconds / 3)

        def heartbeat() -> None:
            while not heartbeat_stop.wait(heartbeat_interval):
                try:
                    renewed = self._repository.renew_job_lease(
                        job_id, self.worker_id, self._lease_seconds
                    )
                except Exception:
                    renewed = False
                if not renewed:
                    lease_lost.set()
                    return

        heartbeat_thread = threading.Thread(
            target=heartbeat,
            name=f"job-heartbeat-{job_id}",
            daemon=True,
        )
        heartbeat_thread.start()
        self._repository.append_event(
            project_id=run.project_id,
            run_id=run_id,
            event_type="RunStageStarted",
            severity="info",
            payload={
                "stage": "running",
                "stage_progress": None,
                "attempt": int(claimed.get("attempt", 1)),
            },
        )
        result = None
        execution_error: str | None = None
        try:
            result = self._executor.execute(
                payload,
                is_cancelled=lambda: (
                    lease_lost.is_set() or self._repository.is_cancel_requested(run_id)
                ),
            )
        except Exception as exc:
            execution_error = f"executor raised {type(exc).__name__}"
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=max(heartbeat_interval * 2, 0.2))

        try:
            still_owned = self._repository.renew_job_lease(
                job_id, self.worker_id, self._lease_seconds
            )
        except Exception:
            still_owned = False
        if lease_lost.is_set() or not still_owned:
            self._record_lease_lost(run.project_id, run_id, job_id)
            return True
        run = self._repository.get_run(run_id)

        if result is None:
            self._repository.append_event(
                project_id=run.project_id,
                run_id=run_id,
                event_type="RunStageFinished",
                severity="error",
                payload={"stage": "running", "stage_progress": 1.0, "status": "failed"},
            )
            finalized = self._repository.finalize_job_failure(
                job_id,
                self.worker_id,
                requested_state=WorkflowState.FAILED_RETRYABLE,
                error=execution_error or "executor failed without a result",
                actor=self.worker_id,
            )
            if not finalized:
                self._record_lease_lost(run.project_id, run_id, job_id)
            return True

        if result.status == "cancelled":
            self._repository.append_event(
                project_id=run.project_id,
                run_id=run_id,
                event_type="RunStageFinished",
                severity="warning",
                payload={"stage": "running", "stage_progress": 1.0, "status": "cancelled"},
            )
            if not self._repository.finalize_job_cancel(
                job_id,
                self.worker_id,
                error=result.error,
                actor=self.worker_id,
            ):
                self._record_lease_lost(run.project_id, run_id, job_id)
            return True

        if result.status == "succeeded":
            self._repository.append_event(
                project_id=run.project_id,
                run_id=run_id,
                event_type="RunStageFinished",
                severity="info",
                payload={"stage": "running", "stage_progress": 1.0, "status": "succeeded"},
            )
            try:
                finalized = self._repository.finalize_job_success(
                    job_id,
                    self.worker_id,
                    result=result.output,
                    artifacts=tuple(result.artifacts),
                    actor=self.worker_id,
                )
            except Exception as exc:
                finalized = self._repository.finalize_job_failure(
                    job_id,
                    self.worker_id,
                    requested_state=WorkflowState.FAILED_TERMINAL,
                    error=f"artifact finalization failed: {type(exc).__name__}",
                    actor=self.worker_id,
                )
                if not finalized:
                    self._record_lease_lost(run.project_id, run_id, job_id)
                return True
            if not finalized:
                self._record_lease_lost(run.project_id, run_id, job_id)
            return True

        error = result.error or "executor failed"
        self._repository.append_event(
            project_id=run.project_id,
            run_id=run_id,
            event_type="RunStageFinished",
            severity="error",
            payload={"stage": "running", "stage_progress": 1.0, "status": result.status},
        )
        if result.status == "timed_out":
            target = WorkflowState.TIMED_OUT
        elif result.status == "failed_retryable":
            target = WorkflowState.FAILED_RETRYABLE
        else:
            target = WorkflowState.FAILED_TERMINAL

        finalized = self._repository.finalize_job_failure(
            job_id,
            self.worker_id,
            requested_state=target,
            error=error,
            actor=self.worker_id,
        )
        if not finalized:
            self._record_lease_lost(run.project_id, run_id, job_id)
        return True

    def _record_lease_lost(self, project_id: str, run_id: str, job_id: str) -> None:
        self._repository.append_event(
            project_id=project_id,
            run_id=run_id,
            event_type="JobLeaseLost",
            severity="error",
            payload={
                "job_id": job_id,
                "worker_id": self.worker_id,
                "artifacts_registered": False,
            },
        )


def main() -> None:
    """Console-script shim; implementation stays in the separate process module."""

    from neuroagent.workflow.worker_main import main as worker_main

    worker_main()
