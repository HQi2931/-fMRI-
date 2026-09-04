"""Approved-plan workflow factory and typed ToolRuntime.

The runtime is deliberately transport- and subprocess-agnostic.  A concrete
executor receives a frozen step and typed artifact handles; it never receives
client-authored shell, MATLAB source, or filesystem paths.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from neuroagent.skills.models import ResolvedStep, SkillPlan, stable_hash
from neuroagent.skills.registry import SkillRegistry, SkillRegistryError
from neuroagent.tools.registry import ToolRegistry, ToolRegistryError


class WorkflowPlanRejected(ValueError):
    """Raised when an approved plan no longer matches the execution boundary."""


@dataclass(frozen=True, slots=True)
class RuntimeArtifact:
    """A path-free artifact handle available to one Tool step."""

    artifact_id: str
    artifact_type: str
    manifest_hash: str
    lineage_hash: str


@dataclass(frozen=True, slots=True)
class ToolStepResult:
    """Typed result returned by one registered tool implementation."""

    artifacts: tuple[RuntimeArtifact, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkflowExecutionResult:
    """Validated result of traversing every step in a frozen DAG."""

    artifacts: tuple[RuntimeArtifact, ...]
    completed_step_ids: tuple[str, ...]
    attempt: int


class ToolExecutor(Protocol):
    def execute(
        self,
        step: ResolvedStep,
        inputs: tuple[RuntimeArtifact, ...],
        *,
        attempt: int,
        is_cancelled: Callable[[], bool],
    ) -> ToolStepResult: ...


class EventSink(Protocol):
    def __call__(self, event_type: str, payload: dict[str, object]) -> None: ...


@dataclass(frozen=True, slots=True)
class ApprovedWorkflow:
    plan: SkillPlan
    ordered_steps: tuple[ResolvedStep, ...]


class WorkflowFactory:
    """Revalidate registry locks before creating an executable workflow."""

    def __init__(self, skills: SkillRegistry, tools: ToolRegistry) -> None:
        self._skills = skills
        self._tools = tools

    def from_approved_plan(
        self,
        plan: SkillPlan,
        *,
        approval_plan_hash: str,
        current_environment_hash: str,
    ) -> ApprovedWorkflow:
        if approval_plan_hash != plan.plan_hash:
            raise WorkflowPlanRejected("approval record does not match the frozen plan hash")
        if plan.environment.environment_hash != current_environment_hash:
            raise WorkflowPlanRejected("approved environment lock has drifted")
        if len({step.step_id for step in plan.steps}) != len(plan.steps):
            raise WorkflowPlanRejected("workflow contains duplicate step IDs")

        for lock in plan.skill_locks:
            try:
                current = self._skills.resolve(lock.skill_id, lock.version)
            except SkillRegistryError as exc:
                raise WorkflowPlanRejected(f"Skill lock unavailable: {lock.skill_id}") from exc
            if current.content_hash != lock.content_hash:
                raise WorkflowPlanRejected(f"Skill lock drifted: {lock.skill_id}@{lock.version}")

        step_by_id = {step.step_id: step for step in plan.steps}
        for step in plan.steps:
            missing = set(step.needs) - step_by_id.keys()
            if missing:
                raise WorkflowPlanRejected(
                    f"step {step.step_id} has missing dependencies: {sorted(missing)}"
                )
            try:
                current_tool = self._tools.resolve_capability(step.tool.capability)
            except ToolRegistryError as exc:
                raise WorkflowPlanRejected(
                    f"Tool lock unavailable: {step.tool.capability}"
                ) from exc
            if current_tool != step.tool:
                raise WorkflowPlanRejected(f"Tool lock drifted: {step.tool.capability}")
            if not step.produces:
                raise WorkflowPlanRejected(f"step {step.step_id} must declare produces")

        ordered = _topological_order(plan.steps)
        return ApprovedWorkflow(
            plan=plan, ordered_steps=tuple(step_by_id[item] for item in ordered)
        )


class ToolRuntime:
    """Execute a frozen DAG with step-level evidence and fail-closed outputs."""

    def execute(
        self,
        workflow: ApprovedWorkflow,
        executor: ToolExecutor,
        *,
        initial_artifacts: Mapping[str, RuntimeArtifact],
        attempt: int = 1,
        is_cancelled: Callable[[], bool] = lambda: False,
        emit: EventSink | None = None,
    ) -> WorkflowExecutionResult:
        if attempt < 1:
            raise ValueError("attempt must be positive")
        available = dict(initial_artifacts)
        all_outputs: list[RuntimeArtifact] = []
        completed: list[str] = []
        for step in workflow.ordered_steps:
            if is_cancelled():
                raise WorkflowPlanRejected("workflow cancelled before the next Tool step")
            inputs = _resolve_inputs(step, available)
            _emit(emit, "ToolStepStarted", step, attempt)
            try:
                result = executor.execute(
                    step,
                    inputs,
                    attempt=attempt,
                    is_cancelled=is_cancelled,
                )
                outputs = _validate_outputs(step, result, workflow.plan.input_manifest_hash)
            except Exception as exc:
                _emit(
                    emit,
                    "ToolStepFailed",
                    step,
                    attempt,
                    error=type(exc).__name__,
                )
                raise
            for artifact in outputs:
                if artifact.artifact_id in available:
                    raise WorkflowPlanRejected(f"duplicate artifact ID: {artifact.artifact_id}")
                available[artifact.artifact_id] = artifact
                if artifact.artifact_type in available:
                    raise WorkflowPlanRejected(f"duplicate artifact type: {artifact.artifact_type}")
                available[artifact.artifact_type] = artifact
                all_outputs.append(artifact)
            completed.append(step.step_id)
            _emit(
                emit,
                "ToolStepFinished",
                step,
                attempt,
                artifact_count=len(outputs),
            )
        return WorkflowExecutionResult(
            artifacts=tuple(all_outputs),
            completed_step_ids=tuple(completed),
            attempt=attempt,
        )


def _resolve_inputs(
    step: ResolvedStep, available: Mapping[str, RuntimeArtifact]
) -> tuple[RuntimeArtifact, ...]:
    missing = [name for name in step.consumes if name not in available]
    if missing:
        raise WorkflowPlanRejected(
            f"step {step.step_id} requires unavailable artifacts: {sorted(missing)}"
        )
    return tuple(available[name] for name in step.consumes)


def _validate_outputs(
    step: ResolvedStep, result: ToolStepResult, manifest_hash: str
) -> tuple[RuntimeArtifact, ...]:
    expected = set(step.produces)
    actual = [artifact.artifact_type for artifact in result.artifacts]
    if set(actual) != expected or len(actual) != len(expected):
        raise WorkflowPlanRejected(
            f"step {step.step_id} output contract mismatch: expected {sorted(expected)}, "
            f"received {sorted(actual)}"
        )
    for artifact in result.artifacts:
        if artifact.manifest_hash != manifest_hash:
            raise WorkflowPlanRejected(
                f"step {step.step_id} produced an artifact from a different manifest"
            )
        if not artifact.artifact_id or not artifact.lineage_hash:
            raise WorkflowPlanRejected(f"step {step.step_id} produced incomplete lineage")
    return result.artifacts


def _topological_order(steps: Sequence[ResolvedStep]) -> tuple[str, ...]:
    remaining = {step.step_id: set(step.needs) for step in steps}
    ordered: list[str] = []
    while remaining:
        ready = sorted(step_id for step_id, needs in remaining.items() if not needs)
        if not ready:
            raise WorkflowPlanRejected("workflow graph contains a cycle")
        for step_id in ready:
            ordered.append(step_id)
            del remaining[step_id]
            for needs in remaining.values():
                needs.discard(step_id)
    return tuple(ordered)


def _emit(
    emit: EventSink | None,
    event_type: str,
    step: ResolvedStep,
    attempt: int,
    **extra: object,
) -> None:
    if emit is None:
        return
    emit(
        event_type,
        {
            "step_id": step.step_id,
            "tool_id": step.tool.tool_id,
            "tool_version": step.tool.version,
            "attempt": attempt,
            "step_hash": stable_hash(step.model_dump(mode="json")),
            **extra,
        },
    )
