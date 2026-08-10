"""Deterministic typed MATLAB mock for unit and CI tests."""

from __future__ import annotations

from collections.abc import Callable

from neuroagent.execution.models import (
    MatlabJobResult,
    MatlabJobSpec,
    MatlabJobStatus,
    RenderedJob,
)


class MockMatlabExecutor:
    def execute(
        self,
        job: MatlabJobSpec,
        rendered: RenderedJob,
        *,
        is_cancelled: Callable[[], bool],
        fail: bool = False,
    ) -> MatlabJobResult:
        if is_cancelled():
            status = MatlabJobStatus.CANCELLED
        elif fail:
            status = MatlabJobStatus.FAILED
        else:
            status = MatlabJobStatus.SUCCEEDED
        return MatlabJobResult(
            status=status,
            job_hash=job.job_hash,
            exit_code=0 if status is MatlabJobStatus.SUCCEEDED else None,
            stdout="mock executor",
            stderr="mock failure" if fail else "",
            rendered=rendered,
            # A mock execution validates orchestration only and must not claim
            # that filesystem artifacts were independently verified.
            registered_artifacts=(),
        )
