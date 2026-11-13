"""Domain values governing the controlled execution lifecycle."""

from __future__ import annotations

from enum import StrEnum


class MockOutcome(StrEnum):
    SUCCEED = "succeed"
    FAIL_RETRYABLE = "fail_retryable"
    FAIL_TERMINAL = "fail_terminal"
    TIMEOUT = "timeout"


class ExecutionBackend(StrEnum):
    """Execution control-plane choice for a queued run."""

    MOCK = "mock"
    MATLAB = "matlab"


class ExecutionWorkspaceMode(StrEnum):
    """Where DPABI reads and writes its DataProcessDir."""

    ISOLATED = "isolated"
    IN_PLACE = "in_place"


class QcExecutionMode(StrEnum):
    """User's explicit choice for the post-processing QC gate."""

    REQUIRE = "require"
    SKIP = "skip"
