"""Worker failure diagnostics: local traceback, path-free persisted reason.

The executor-raises path used to persist only ``executor raised <ClassName>``,
which left a real MATLAB failure undiagnosable.  The full traceback now goes to
the local worker log while the run record keeps a redacted, still-actionable
reason, so workstation paths never reach the database, audit trail or SSE
consumers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from neuroagent.application.contracts import RunCreate, WorkflowState
from neuroagent.application.ports import ExecutionResult
from neuroagent.application.services import NeuroAgentService
from neuroagent.bootstrap import build_worker
from neuroagent.execution.matlab import MatlabExecutionError

from .conftest import make_approved_plan, make_project

_PRIVATE_PATH = r"D:\private research\matlab\bin\matlab.exe"


class RaisingExecutor:
    """Executor that fails the way the MATLAB adapter fails on a missing binary."""

    def __init__(self) -> None:
        self.calls = 0

    def execute(
        self,
        payload: dict[str, Any],
        *,
        is_cancelled: Callable[[], bool],
    ) -> ExecutionResult:
        del payload, is_cancelled
        self.calls += 1
        raise MatlabExecutionError(f"MATLAB executable not found: {_PRIVATE_PATH}")


class SucceedingExecutor:
    def execute(
        self,
        payload: dict[str, Any],
        *,
        is_cancelled: Callable[[], bool],
    ) -> ExecutionResult:
        del payload, is_cancelled
        return ExecutionResult(status="succeeded", output={"executor": "synthetic"}, artifacts=())


def _queued_run(
    service: NeuroAgentService,
    source_root: Path,
    work_root: Path,
    *,
    key: str,
    max_attempts: int = 2,
):
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    return service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
            max_attempts=max_attempts,
            mock_outcome="succeed",
        ),
        key,
    )


def test_executor_exception_keeps_retry_budget_and_persists_actionable_reason(
    service: NeuroAgentService,
    source_root: Path,
    work_root: Path,
) -> None:
    run = _queued_run(service, source_root, work_root, key="raising-run")
    worker = build_worker(service, worker_id="worker-raising", executor=RaisingExecutor())

    # Attach to the module logger directly: the assertion is about the worker's
    # own logging behaviour, not about how pytest capture happens to be wired.
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    worker_logger = logging.getLogger("neuroagent.workflow.worker")
    handler = _Capture(level=logging.ERROR)
    worker_logger.addHandler(handler)
    try:
        assert worker.run_once() is True
    finally:
        worker_logger.removeHandler(handler)

    final = service.get_run(run.run_id)
    # The failure taxonomy is unchanged: a raised executor error stays retryable.
    assert final.state is WorkflowState.FAILED_RETRYABLE

    assert final.error is not None
    # Actionable: the operator learns which failure it was.
    assert "MatlabExecutionError" in final.error
    assert "MATLAB executable not found" in final.error
    # Path-free: the persisted reason cannot leak workstation layout.
    assert _PRIVATE_PATH not in final.error
    assert "[REDACTED-PATH]" in final.error

    # The full traceback is still available locally for diagnosis.
    tracebacks = [record for record in records if record.exc_info is not None]
    assert tracebacks, "expected the executor traceback to be logged"
    assert "MATLAB executable not found" in str(tracebacks[0].exc_info[1])


def test_executor_exception_persisted_reason_is_bounded(
    service: NeuroAgentService,
    source_root: Path,
    work_root: Path,
) -> None:
    class VerboseExecutor:
        def execute(
            self,
            payload: dict[str, Any],
            *,
            is_cancelled: Callable[[], bool],
        ) -> ExecutionResult:
            del payload, is_cancelled
            raise MatlabExecutionError("x" * 5_000)

    run = _queued_run(service, source_root, work_root, key="verbose-run")
    worker = build_worker(service, worker_id="worker-verbose", executor=VerboseExecutor())
    assert worker.run_once() is True

    error = service.get_run(run.run_id).error
    assert error is not None
    assert len(error) <= 500 + len("executor_error:MatlabExecutionError: ")


def test_worker_binds_one_trace_id_per_attempt(
    service: NeuroAgentService,
    source_root: Path,
    work_root: Path,
) -> None:
    """Every event of one worker attempt must share a trace id for correlation.

    Events written while the run is created belong to the request context; the
    defect being guarded here is that worker events used to each receive a
    freshly minted id, which made one attempt un-correlatable in the event log.
    """

    run = _queued_run(service, source_root, work_root, key="trace-run", max_attempts=1)
    request_events = len(service.list_run_events(run.run_id))

    worker = build_worker(service, worker_id="worker-trace", executor=SucceedingExecutor())
    assert worker.run_once() is True

    worker_events = service.list_run_events(run.run_id)[request_events:]
    assert len(worker_events) >= 2, "expected at least a start and a finish event"
    trace_ids = {event.trace_id for event in worker_events}
    assert len(trace_ids) == 1, f"expected one correlated trace id, got {sorted(trace_ids)}"
    # Matches the width of runtime_events.trace_id (String(36)).
    assert len(next(iter(trace_ids))) <= 36
