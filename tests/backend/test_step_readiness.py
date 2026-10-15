from __future__ import annotations

from inspect import signature

import pytest
from pydantic import ValidationError

from neuroagent.application.automation.models import (
    AuthorityReferences,
    TaskBudget,
    TaskStatus,
    TaskStep,
    TaskStepStatus,
    WaitingCondition,
    WaitingKind,
    WorkGoal,
    WorkTask,
)
from neuroagent.application.automation.step_readiness import (
    StepReadinessAssessment,
    StepReadinessEvidenceSummary,
    StepReadinessHandler,
    StepReadinessOutcome,
    StepReadinessRegistry,
    StepReadinessResultKind,
    StepReadinessStepKind,
)


def make_bound_task(*, kind: str = "preprocessing_run", run_id: str = "run-1") -> WorkTask:
    step = TaskStep(
        step_id="step-1",
        sequence=0,
        kind=kind,
        status=TaskStepStatus.WAITING,
        authority_refs=AuthorityReferences(workflow_run_id=run_id),
        version=3,
    )
    return WorkTask(
        task_id="task-1",
        goal=WorkGoal(
            description="Complete the requested fMRI workflow",
            completion_conditions=("verified outputs are available",),
        ),
        status=TaskStatus.WAITING_RUN,
        waiting_condition=WaitingCondition(
            kind=WaitingKind.WORKFLOW_RUN,
            reference=run_id,
            reason="The authorized workflow run is still being checked.",
        ),
        authority_refs=AuthorityReferences(workflow_run_id=run_id),
        steps=(step,),
        budget=TaskBudget(
            max_steps=10,
            max_model_calls=4,
            max_tokens=2000,
            max_elapsed_seconds=1800,
        ),
        version=7,
    )


def make_evidence(
    *,
    step_kind: StepReadinessStepKind = StepReadinessStepKind.PREPROCESSING_RUN,
    result_kind: StepReadinessResultKind = StepReadinessResultKind.PREPROCESSING_ARTIFACTS,
) -> StepReadinessEvidenceSummary:
    return StepReadinessEvidenceSummary(
        step_kind=step_kind,
        run_version=4,
        plan_revision_id="plan-revision-4",
        frozen_input_fingerprint="a" * 64,
        result_kind=result_kind,
        result_identity="result-bundle-4",
        lineage_fingerprint="b" * 64,
        result_version=2 if step_kind is StepReadinessStepKind.STATISTICAL_RUN else None,
        result_bundle_fingerprint=(
            "c" * 64 if step_kind is StepReadinessStepKind.STATISTICAL_RUN else None
        ),
    )


def make_assessment(
    task: WorkTask,
    step: TaskStep,
    run_id: str,
    *,
    outcome: StepReadinessOutcome = StepReadinessOutcome.WAITING_RUN,
    evidence: StepReadinessEvidenceSummary | None = None,
) -> StepReadinessAssessment:
    waiting_condition = None
    if outcome is StepReadinessOutcome.WAITING_RUN:
        waiting_condition = WaitingCondition(
            kind=WaitingKind.WORKFLOW_RUN,
            reference=run_id,
            reason="The authorized workflow run is still being checked.",
        )
    elif outcome is StepReadinessOutcome.WAITING_USER:
        waiting_condition = WaitingCondition(
            kind=WaitingKind.USER_DECISION,
            reference=f"qc:{run_id}",
            reason="The user must decide the QC result.",
        )
    elif outcome is StepReadinessOutcome.WAITING_DEPENDENCY:
        reference = (
            f"statistical-result:{run_id}"
            if step.kind == "statistical_run"
            else f"downstream:{run_id}"
        )
        waiting_condition = WaitingCondition(
            kind=WaitingKind.DEPENDENCY,
            reference=reference,
            reason="A required result is not ready yet.",
        )
    return StepReadinessAssessment(
        task_id=task.task_id,
        task_version=task.version,
        step_id=step.step_id,
        step_version=step.version,
        step_kind=step.kind,
        run_id=run_id,
        outcome=outcome,
        reason_code="run_still_active",
        reason="The authorized workflow run is still active.",
        waiting_condition=waiting_condition,
        evidence=evidence,
    )


class StubHandler:
    def __init__(self, kind: str, *, wrong_task_version: bool = False) -> None:
        self._kind = kind
        self.wrong_task_version = wrong_task_version
        self.calls = 0

    @property
    def kind(self) -> str:
        return self._kind

    def inspect(self, task: WorkTask, step: TaskStep, run_id: str) -> StepReadinessAssessment:
        self.calls += 1
        assessment = make_assessment(task, step, run_id)
        if self.wrong_task_version:
            return assessment.model_copy(update={"task_version": task.version + 1})
        return assessment


def test_readiness_outcomes_are_version_bound_and_immutable() -> None:
    task = make_bound_task()
    step = task.steps[0]
    assessment = make_assessment(task, step, "run-1")

    assert assessment.task_version == task.version
    assert assessment.step_version == step.version
    with pytest.raises(ValidationError, match="frozen"):
        assessment.outcome = StepReadinessOutcome.BLOCKED  # type: ignore[misc]


def test_ready_to_finalize_requires_matching_constrained_evidence() -> None:
    evidence = make_evidence()
    task = make_bound_task()
    step = task.steps[0]
    ready = StepReadinessAssessment(
        **{
            **make_assessment(task, step, "run-1").model_dump(),
            "outcome": StepReadinessOutcome.READY_TO_FINALIZE,
            "waiting_condition": None,
            "evidence": evidence,
        }
    )
    assert ready.evidence == evidence

    with pytest.raises(ValidationError, match="requires verified evidence"):
        make_assessment(task, step, "run-1", outcome=StepReadinessOutcome.READY_TO_FINALIZE)
    with pytest.raises(ValidationError, match="match the assessment step kind"):
        StepReadinessAssessment(
            **{
                **make_assessment(task, step, "run-1").model_dump(),
                "outcome": StepReadinessOutcome.READY_TO_FINALIZE,
                "waiting_condition": None,
                "evidence": make_evidence(
                    step_kind=StepReadinessStepKind.STATISTICAL_RUN,
                    result_kind=StepReadinessResultKind.STATISTICAL_RESULT,
                ),
            }
        )
    with pytest.raises(ValidationError, match="only ready_to_finalize"):
        make_assessment(task, step, "run-1", evidence=evidence)


def test_evidence_summary_rejects_unbounded_or_cross_type_identity() -> None:
    with pytest.raises(ValidationError, match="frozen_input_fingerprint"):
        StepReadinessEvidenceSummary(
            **{
                **make_evidence().model_dump(),
                "frozen_input_fingerprint": "not-a-hash",
            }
        )

    with pytest.raises(ValidationError, match="result kind must match"):
        make_evidence(result_kind=StepReadinessResultKind.STATISTICAL_RESULT)

    with pytest.raises(ValidationError, match="result_identity"):
        StepReadinessEvidenceSummary(
            **{
                **make_evidence().model_dump(),
                "result_identity": "C:\\private\\result.nii",
            }
        )

    with pytest.raises(ValidationError, match="result version and bundle fingerprint"):
        StepReadinessEvidenceSummary(
            **{
                **make_evidence(
                    step_kind=StepReadinessStepKind.STATISTICAL_RUN,
                    result_kind=StepReadinessResultKind.STATISTICAL_RESULT,
                ).model_dump(),
                "result_version": None,
                "result_bundle_fingerprint": None,
            }
        )

    with pytest.raises(ValidationError, match="extra_forbidden"):
        StepReadinessEvidenceSummary(
            **{**make_evidence().model_dump(), "artifact_path": "private/result.nii"}
        )


def test_waiting_condition_and_safe_reason_are_validated() -> None:
    task = make_bound_task()
    step = task.steps[0]
    with pytest.raises(ValidationError, match="waiting condition kind"):
        StepReadinessAssessment(
            **{
                **make_assessment(task, step, "run-1").model_dump(),
                "waiting_condition": WaitingCondition(
                    kind=WaitingKind.USER_DECISION,
                    reference="qc:run-1",
                    reason="The user must review QC.",
                ),
            }
        )
    with pytest.raises(ValidationError, match="require a waiting condition"):
        StepReadinessAssessment(
            **{
                **make_assessment(task, step, "run-1").model_dump(),
                "waiting_condition": None,
            }
        )
    with pytest.raises(ValidationError, match="reference must match"):
        StepReadinessAssessment(
            **{
                **make_assessment(task, step, "run-1").model_dump(),
                "waiting_condition": WaitingCondition(
                    kind=WaitingKind.WORKFLOW_RUN,
                    reference="run-2",
                    reason="The authorized workflow run is still being checked.",
                ),
            }
        )
    with pytest.raises(ValidationError, match="only waiting outcomes"):
        StepReadinessAssessment(
            **{
                **make_assessment(task, step, "run-1").model_dump(),
                "outcome": StepReadinessOutcome.BLOCKED,
                "waiting_condition": task.waiting_condition,
            }
        )
    with pytest.raises(ValidationError, match="reason"):
        StepReadinessAssessment(
            **{**make_assessment(task, step, "run-1").model_dump(), "reason": "bad\nreason"}
        )


def test_registry_dispatches_by_exact_kind_and_rejects_duplicates() -> None:
    preprocessing = StubHandler("preprocessing_run")
    statistical = StubHandler("statistical_run")
    registry = StepReadinessRegistry((preprocessing, statistical))
    task = make_bound_task()

    result = registry.inspect(task, task.steps[0], "run-1")

    assert result.outcome is StepReadinessOutcome.WAITING_RUN
    assert preprocessing.calls == 1
    assert statistical.calls == 0
    with pytest.raises(ValueError, match="already registered"):
        registry.register(StubHandler("preprocessing_run"))


def test_missing_handler_returns_indeterminate_without_default_dispatch() -> None:
    registered = StubHandler("statistical_run")
    registry = StepReadinessRegistry((registered,))
    task = make_bound_task(kind="Preprocessing_run")

    result = registry.inspect(task, task.steps[0], "run-1")

    assert result.outcome is StepReadinessOutcome.INDETERMINATE
    assert result.reason_code == "readiness_handler_missing"
    assert result.step_kind == "Preprocessing_run"
    assert registered.calls == 0


def test_registry_fails_closed_on_unresolved_or_stale_identity() -> None:
    handler = StubHandler("preprocessing_run")
    registry = StepReadinessRegistry((handler,))
    task = make_bound_task()

    wrong_run = registry.inspect(task, task.steps[0], "run-2")
    stale_step = task.steps[0].model_copy(update={"version": task.steps[0].version + 1})
    wrong_step = registry.inspect(task, stale_step, "run-1")

    assert wrong_run.outcome is StepReadinessOutcome.INDETERMINATE
    assert wrong_step.outcome is StepReadinessOutcome.INDETERMINATE
    assert wrong_run.reason_code == wrong_step.reason_code == "step_identity_unresolved"
    assert handler.calls == 0


def test_registry_rejects_assessments_for_a_different_task_version() -> None:
    handler = StubHandler("preprocessing_run", wrong_task_version=True)
    registry = StepReadinessRegistry((handler,))
    task = make_bound_task()

    result = registry.inspect(task, task.steps[0], "run-1")

    assert result.outcome is StepReadinessOutcome.INDETERMINATE
    assert result.reason_code == "readiness_assessment_identity_mismatch"


def test_handler_inspect_contract_has_no_write_port_argument() -> None:
    assert tuple(signature(StepReadinessHandler.inspect).parameters) == (
        "self",
        "task",
        "step",
        "run_id",
    )
