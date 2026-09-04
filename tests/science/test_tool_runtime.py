from __future__ import annotations

import pytest

from neuroagent.skills.models import EnvironmentSnapshot, ResolvedStep, SkillPlan
from neuroagent.tools.registry import build_default_tool_registry
from neuroagent.workflow.runtime import (
    RuntimeArtifact,
    ToolRuntime,
    ToolStepResult,
    WorkflowFactory,
    WorkflowPlanRejected,
)


class _Skill:
    content_hash = "s" * 64


class _Skills:
    def resolve(self, skill_id: str, version: str):
        assert (skill_id, version) == ("test.skill", "1.0.0")
        return _Skill()


def _plan(*, cycle: bool = False) -> SkillPlan:
    tool = build_default_tool_registry().resolve_capability("fmri.manifest.verify")
    first = ResolvedStep(
        step_id="first",
        tool=tool,
        needs=("second",) if cycle else (),
        consumes=("input",),
        produces=("verified",),
        parameter_names=(),
        qc_gate=False,
    )
    second = ResolvedStep(
        step_id="second",
        tool=tool,
        needs=("first",),
        consumes=("verified",),
        produces=("output",),
        parameter_names=(),
        qc_gate=True,
    )
    return SkillPlan(
        plan_id="plan-1",
        project_id="project-1",
        dataset_ref="dataset-1",
        input_manifest_hash="m" * 64,
        input_artifact_id="input-1",
        base_cfg_artifact_id=None,
        preprocessing_parameters_hash=None,
        skill_locks=({"skill_id": "test.skill", "version": "1.0.0", "content_hash": "s" * 64},),
        resolved_parameters=(),
        environment=EnvironmentSnapshot(
            matlab_version="R2023b",
            spm_version="SPM12",
            dpabi_version="V8.2",
            adapter_version="1.0.0",
            environment_hash="e" * 64,
        ),
        steps=(first, second),
        artifact_expectations=("verified", "output"),
        qc_gates=("manual",),
        approval_requirements=("plan_hash",),
        warnings=(),
        plan_hash="p" * 64,
    )


class _Executor:
    def execute(self, step, inputs, *, attempt, is_cancelled):
        assert not is_cancelled()
        return ToolStepResult(
            artifacts=(
                RuntimeArtifact(
                    artifact_id=f"{step.step_id}-{attempt}",
                    artifact_type=step.produces[0],
                    manifest_hash="m" * 64,
                    lineage_hash="l" * 64,
                ),
            )
        )


def test_runtime_topologically_executes_every_frozen_step_and_emits_events() -> None:
    plan = _plan()
    workflow = WorkflowFactory(_Skills(), build_default_tool_registry()).from_approved_plan(
        plan,
        approval_plan_hash=plan.plan_hash,
        current_environment_hash=plan.environment.environment_hash,
    )
    events: list[str] = []
    result = ToolRuntime().execute(
        workflow,
        _Executor(),
        initial_artifacts={"input": RuntimeArtifact("input-1", "input", "m" * 64, "l" * 64)},
        emit=lambda event_type, _payload: events.append(event_type),
    )
    assert result.completed_step_ids == ("first", "second")
    assert events == [
        "ToolStepStarted",
        "ToolStepFinished",
        "ToolStepStarted",
        "ToolStepFinished",
    ]


def test_runtime_rejects_cycles_and_output_contract_mismatch() -> None:
    with pytest.raises(WorkflowPlanRejected, match="cycle"):
        WorkflowFactory(_Skills(), build_default_tool_registry()).from_approved_plan(
            _plan(cycle=True),
            approval_plan_hash="p" * 64,
            current_environment_hash="e" * 64,
        )

    plan = _plan()
    workflow = WorkflowFactory(_Skills(), build_default_tool_registry()).from_approved_plan(
        plan,
        approval_plan_hash=plan.plan_hash,
        current_environment_hash=plan.environment.environment_hash,
    )

    class BadExecutor(_Executor):
        def execute(self, step, inputs, *, attempt, is_cancelled):
            return ToolStepResult()

    with pytest.raises(WorkflowPlanRejected, match="output contract"):
        ToolRuntime().execute(
            workflow,
            BadExecutor(),
            initial_artifacts={"input": RuntimeArtifact("input-1", "input", "m" * 64, "l" * 64)},
        )
