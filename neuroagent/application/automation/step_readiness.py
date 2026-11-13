"""Read-only readiness contracts for explicit WorkTask steps.

Handlers inspect authoritative records and return a version-bound assessment.
The application coordinator remains responsible for status transitions,
compare-and-swap persistence, fencing, and outbox finalization.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from neuroagent.application.automation.models import (
    TaskStep,
    WaitingCondition,
    WaitingKind,
    WorkTask,
)


class StepReadinessOutcome(StrEnum):
    WAITING_RUN = "waiting_run"
    WAITING_USER = "waiting_user"
    WAITING_DEPENDENCY = "waiting_dependency"
    READY_TO_FINALIZE = "ready_to_finalize"
    BLOCKED = "blocked"
    INDETERMINATE = "indeterminate"


class StepReadinessStepKind(StrEnum):
    PREPROCESSING_RUN = "preprocessing_run"
    STATISTICAL_RUN = "statistical_run"


class StepReadinessResultKind(StrEnum):
    PREPROCESSING_ARTIFACTS = "preprocessing_artifacts"
    STATISTICAL_RESULT = "statistical_result"


class StepReadinessEvidenceSummary(BaseModel):
    """Bounded identity fingerprints; deliberately excludes paths and payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_kind: StepReadinessStepKind
    run_version: int = Field(ge=1)
    plan_revision_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")
    frozen_input_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    result_kind: StepReadinessResultKind
    result_identity: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    lineage_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    result_version: int | None = Field(default=None, ge=1)
    result_bundle_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def result_kind_matches_step(self) -> StepReadinessEvidenceSummary:
        expected_result_kind = {
            StepReadinessStepKind.PREPROCESSING_RUN: (
                StepReadinessResultKind.PREPROCESSING_ARTIFACTS
            ),
            StepReadinessStepKind.STATISTICAL_RUN: StepReadinessResultKind.STATISTICAL_RESULT,
        }[self.step_kind]
        if self.result_kind is not expected_result_kind:
            raise ValueError("evidence result kind must match its step kind")
        if self.step_kind is StepReadinessStepKind.STATISTICAL_RUN:
            if self.result_version is None or self.result_bundle_fingerprint is None:
                raise ValueError(
                    "statistical evidence requires result version and bundle fingerprint"
                )
        elif self.result_version is not None or self.result_bundle_fingerprint is not None:
            raise ValueError("preprocessing evidence cannot carry statistical result fields")
        return self


class StepReadinessAssessment(BaseModel):
    """Immutable, version-bound inspection result; not a state transition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1, max_length=100)
    task_version: int = Field(ge=1)
    step_id: str = Field(min_length=1, max_length=100)
    step_version: int = Field(ge=1)
    step_kind: str = Field(min_length=1, max_length=100)
    run_id: str = Field(min_length=1, max_length=100)
    outcome: StepReadinessOutcome
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    # Handler implementations must provide a short, pre-sanitized user-facing
    # explanation, never raw exception text, filesystem paths, or authority data.
    reason: str = Field(min_length=1, max_length=1000, pattern=r"^[^\x00-\x1f\x7f]+$")
    waiting_condition: WaitingCondition | None = None
    evidence: StepReadinessEvidenceSummary | None = None

    @model_validator(mode="after")
    def payload_matches_outcome(self) -> StepReadinessAssessment:
        waiting_kinds = {
            StepReadinessOutcome.WAITING_RUN: WaitingKind.WORKFLOW_RUN,
            StepReadinessOutcome.WAITING_USER: WaitingKind.USER_DECISION,
            StepReadinessOutcome.WAITING_DEPENDENCY: WaitingKind.DEPENDENCY,
        }
        expected_waiting_kind = waiting_kinds.get(self.outcome)
        if expected_waiting_kind is not None and self.waiting_condition is None:
            raise ValueError("waiting outcomes require a waiting condition")
        if expected_waiting_kind is None and self.waiting_condition is not None:
            raise ValueError("only waiting outcomes may carry a waiting condition")
        if (
            self.waiting_condition is not None
            and self.waiting_condition.kind is not expected_waiting_kind
        ):
            raise ValueError("waiting condition kind must match the readiness outcome")
        if self.waiting_condition is not None:
            expected_reference = {
                StepReadinessOutcome.WAITING_RUN: self.run_id,
                StepReadinessOutcome.WAITING_USER: f"qc:{self.run_id}",
                StepReadinessOutcome.WAITING_DEPENDENCY: {
                    StepReadinessStepKind.PREPROCESSING_RUN.value: f"downstream:{self.run_id}",
                    StepReadinessStepKind.STATISTICAL_RUN.value: (
                        f"statistical-result:{self.run_id}"
                    ),
                }.get(self.step_kind),
            }[self.outcome]
            if expected_reference is None or self.waiting_condition.reference != expected_reference:
                raise ValueError("waiting condition reference must match the assessed step and Run")
        if self.outcome is StepReadinessOutcome.READY_TO_FINALIZE:
            if self.evidence is None:
                raise ValueError("ready_to_finalize requires verified evidence summary")
            if self.evidence.step_kind.value != self.step_kind:
                raise ValueError("evidence step kind must match the assessment step kind")
        elif self.evidence is not None:
            raise ValueError("only ready_to_finalize may carry an evidence summary")
        return self


class StepReadinessHandler(Protocol):
    """Read-only inspection contract; dependencies are captured by the handler."""

    @property
    def kind(self) -> str: ...

    def inspect(self, task: WorkTask, step: TaskStep, run_id: str) -> StepReadinessAssessment: ...


class StepReadinessRegistry:
    """Dispatch by exact step kind, with no implicit/default handler."""

    def __init__(self, handlers: Iterable[StepReadinessHandler] = ()) -> None:
        self._handlers: dict[str, StepReadinessHandler] = {}
        for handler in handlers:
            self.register(handler)

    def register(self, handler: StepReadinessHandler) -> None:
        kind = handler.kind
        if not kind or not kind.strip() or len(kind) > 100:
            raise ValueError("readiness handler kind must be a bounded non-empty exact string")
        if kind in self._handlers:
            raise ValueError(f"readiness handler already registered for kind: {kind}")
        self._handlers[kind] = handler

    def inspect(self, task: WorkTask, step: TaskStep, run_id: str) -> StepReadinessAssessment:
        if not run_id:
            raise ValueError("run_id is required for readiness inspection")
        if (
            len(task.steps) != 1
            or task.steps[0] != step
            or task.authority_refs.workflow_run_id != run_id
            or step.authority_refs.workflow_run_id != run_id
        ):
            return self._indeterminate(
                task,
                step,
                run_id,
                "step_identity_unresolved",
                "The current step and Run binding could not be verified.",
            )

        handler = self._handlers.get(step.kind)
        if handler is None:
            return self._indeterminate(
                task,
                step,
                run_id,
                "readiness_handler_missing",
                "No readiness checker is registered for this step type.",
            )

        assessment = handler.inspect(task, step, run_id)
        if (
            assessment.task_id != task.task_id
            or assessment.task_version != task.version
            or assessment.step_id != step.step_id
            or assessment.step_version != step.version
            or assessment.step_kind != step.kind
            or assessment.run_id != run_id
        ):
            return self._indeterminate(
                task,
                step,
                run_id,
                "readiness_assessment_identity_mismatch",
                "The readiness checker returned an assessment for a different task state.",
            )
        return assessment

    @staticmethod
    def _indeterminate(
        task: WorkTask,
        step: TaskStep,
        run_id: str,
        reason_code: str,
        reason: str,
    ) -> StepReadinessAssessment:
        return StepReadinessAssessment(
            task_id=task.task_id,
            task_version=task.version,
            step_id=step.step_id,
            step_version=step.version,
            step_kind=step.kind,
            run_id=run_id,
            outcome=StepReadinessOutcome.INDETERMINATE,
            reason_code=reason_code,
            reason=reason,
        )
