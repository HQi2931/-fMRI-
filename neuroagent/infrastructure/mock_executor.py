"""Deterministic executor used by tests and the initial vertical slice."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from neuroagent.application.hashing import content_hash
from neuroagent.application.ports import ExecutionResult
from neuroagent.skills.models import ResolvedStep, SkillPlan, stable_hash
from neuroagent.workflow.runtime import (
    RuntimeArtifact,
    ToolRuntime,
    ToolStepResult,
    WorkflowFactory,
)


class _SyntheticToolExecutor:
    def execute(
        self,
        step: ResolvedStep,
        inputs: tuple[RuntimeArtifact, ...],
        *,
        attempt: int,
        is_cancelled: Callable[[], bool],
    ) -> ToolStepResult:
        if is_cancelled():
            raise RuntimeError("mock workflow cancelled")
        manifest_hash = inputs[0].manifest_hash if inputs else "0" * 64
        artifacts = tuple(
            RuntimeArtifact(
                artifact_id=f"mock-{attempt}-{step.step_id}-{index}",
                artifact_type=artifact_type,
                manifest_hash=manifest_hash,
                lineage_hash=stable_hash(
                    {
                        "step": step.step_id,
                        "tool": step.tool.tool_id,
                        "attempt": attempt,
                        "artifact_type": artifact_type,
                    }
                ),
            )
            for index, artifact_type in enumerate(step.produces)
        )
        return ToolStepResult(artifacts=artifacts)


class MockJobExecutor:
    def __init__(self, workflow_factory: WorkflowFactory | None = None) -> None:
        self._workflow_factory = workflow_factory

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
        if payload.get("executor_type", "workflow_mock") != "workflow_mock":
            return ExecutionResult(
                status="failed_terminal", error="requested executor type is not registered"
            )

        workflow_payload = payload.get("workflow_plan")
        if workflow_payload is not None:
            if self._workflow_factory is None:
                return ExecutionResult(
                    status="failed_terminal", error="workflow runtime is not configured"
                )
            try:
                plan = SkillPlan.model_validate(workflow_payload)
                workflow = self._workflow_factory.from_approved_plan(
                    plan,
                    approval_plan_hash=str(payload.get("plan_hash", "")),
                    current_environment_hash=plan.environment.environment_hash,
                )
                initial = {
                    name: RuntimeArtifact(
                        artifact_id=f"input-{name}",
                        artifact_type=name,
                        manifest_hash=plan.input_manifest_hash,
                        lineage_hash=stable_hash({"input": name, "plan": plan.plan_hash}),
                    )
                    for step in workflow.ordered_steps
                    for name in step.consumes
                    if name
                    not in {
                        produced for item in workflow.ordered_steps for produced in item.produces
                    }
                }
                result = ToolRuntime().execute(
                    workflow,
                    _SyntheticToolExecutor(),
                    initial_artifacts=initial,
                    is_cancelled=is_cancelled,
                )
            except Exception as exc:
                return ExecutionResult(
                    status="failed_terminal",
                    error=f"workflow validation failed: {type(exc).__name__}",
                )
            return ExecutionResult(
                status="succeeded",
                output={
                    "executor": "workflow_mock",
                    "validated": True,
                    "completed_steps": result.completed_step_ids,
                },
                artifacts=tuple(
                    {
                        "artifact_type": artifact.artifact_type,
                        "relative_path": f"output/{artifact.artifact_id}.json",
                        "checksum": stable_hash(
                            artifact.__dict__
                            if hasattr(artifact, "__dict__")
                            else {
                                "artifact_id": artifact.artifact_id,
                                "artifact_type": artifact.artifact_type,
                                "manifest_hash": artifact.manifest_hash,
                                "lineage_hash": artifact.lineage_hash,
                            }
                        ),
                        "size_bytes": 1,
                        "provenance": {
                            "executor": "workflow_mock",
                            "lineage_hash": artifact.lineage_hash,
                        },
                    }
                    for artifact in result.artifacts
                ),
            )

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
