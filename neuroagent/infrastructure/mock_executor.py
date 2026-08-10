"""Deterministic executor used by tests and the initial vertical slice."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from neuroagent.application.hashing import content_hash
from neuroagent.application.ports import ExecutionResult


class MockJobExecutor:
    def execute(
        self,
        payload: dict[str, Any],
        *,
        is_cancelled: Callable[[], bool],
    ) -> ExecutionResult:
        delay_ms = int(payload.get("delay_ms", 0))
        elapsed = 0
        while elapsed < delay_ms:
            if is_cancelled():
                return ExecutionResult(status="cancelled", error="cancel requested")
            interval = min(20, delay_ms - elapsed)
            time.sleep(interval / 1000)
            elapsed += interval
        if is_cancelled():
            return ExecutionResult(status="cancelled", error="cancel requested")

        outcome = str(payload.get("outcome", "succeed"))
        if outcome == "fail_retryable":
            return ExecutionResult(status="failed_retryable", error="mock retryable failure")
        if outcome == "fail_terminal":
            return ExecutionResult(status="failed_terminal", error="mock terminal failure")
        if outcome == "timeout":
            return ExecutionResult(status="timed_out", error="mock timeout")
        result_payload = {"executor": "mock", "validated": True}
        checksum = content_hash(result_payload)
        return ExecutionResult(
            status="succeeded",
            output=result_payload,
            artifacts=(
                {
                    "artifact_type": "mock.result",
                    "relative_path": "output/mock-result.json",
                    "checksum": checksum,
                    "size_bytes": len(str(result_payload).encode("utf-8")),
                    "provenance": {"executor": "mock", "payload_hash": content_hash(payload)},
                },
            ),
        )
