from __future__ import annotations

import pytest
from pydantic import ValidationError

from neuroagent.domain.fmri.skillpacks.builtin import builtin_skill_specs
from neuroagent.execution.preprocessing import (
    DPARSFA_COMPUTE_CAPABILITIES,
    DPARSFA_EXECUTION_UNIT_CAPABILITIES,
    OTHER_EXECUTION_CAPABILITIES,
    PREPROCESSING_OUTSIDE_UNIT_CAPABILITIES,
    PreprocessingCompilation,
    validate_preprocessing_capability_coverage,
    validate_preprocessing_execution_unit,
)
from neuroagent.execution.units import (
    ArtifactPort,
    ExecutionGate,
    ExecutionUnit,
    ExecutionUnitKind,
    GateOutcome,
    GatePosition,
    VerificationMaturity,
    VerificationStage,
)
from neuroagent.skills.models import ResolvedStep, ResolvedToolRef
from neuroagent.tools import build_default_tool_registry
from neuroagent.tools.models import ToolRisk
from tests.science.test_preprocessing_compilation import (
    common_parameters,
    compile_fixture,
    frozen_plan,
    manifest_for,
    rehash,
)
from tests.science.test_skill_compiler import request


def unit_payload() -> dict[str, object]:
    return {
        "unit_id": "preprocess-metrics",
        "kind": ExecutionUnitKind.DPARSFA_SINGLE_CALL,
        "logical_step_ids": ("preprocess_common", "calculate_alff", "calculate_reho"),
        "inputs": (
            ArtifactPort(
                artifact_ref="manifest:dataset-1",
                artifact_type="input.subject_manifest",
                verification_stage=VerificationStage.BEFORE_CALL,
            ),
        ),
        "outputs": (
            ArtifactPort(
                artifact_ref="run-1:timeseries",
                artifact_type="preprocessing.functional_timeseries",
                verification_stage=VerificationStage.AFTER_CALL,
                reusable_checkpoint=True,
            ),
            ArtifactPort(
                artifact_ref="run-1:alff",
                artifact_type="metric.alff.z_score",
                verification_stage=VerificationStage.AFTER_CALL,
            ),
        ),
        "gates": (
            ExecutionGate(
                gate_id="approved-inputs",
                position=GatePosition.BEFORE_CALL,
                outcome=GateOutcome.BLOCK_DISPATCH,
                evidence_required=("plan_hash", "input_manifest_hash", "environment_hash"),
            ),
            ExecutionGate(
                gate_id="outputs-and-lineage",
                position=GatePosition.AFTER_CALL,
                outcome=GateOutcome.FAIL_UNIT,
                evidence_required=("actual_files", "metadata", "sha256"),
            ),
        ),
        "verification_maturity": VerificationMaturity.CONTRACT_ONLY,
    }


def test_execution_unit_models_one_physical_call_for_multiple_logical_steps() -> None:
    unit = ExecutionUnit.model_validate(unit_payload())

    assert unit.dpabi_entrypoint == "DPARSFA_run"
    assert unit.invocation_count == 1
    assert len(unit.logical_step_ids) == 3
    assert unit.retry_owner == "workflow_worker"
    assert unit.retry_scope == "whole_unit"
    assert unit.retry_workspace == "fresh_attempt"
    assert unit.resume_strategy == "restart_unit"
    assert {gate.position for gate in unit.gates} == {
        GatePosition.BEFORE_CALL,
        GatePosition.AFTER_CALL,
    }


def test_checkpoint_means_verified_artifact_reuse_not_a_pause_point() -> None:
    checkpoint = ArtifactPort(
        artifact_ref="artifact:verified-timeseries",
        artifact_type="preprocessing.functional_timeseries",
        verification_stage=VerificationStage.BEFORE_CALL,
        reusable_checkpoint=True,
    )

    assert checkpoint.reusable_checkpoint
    assert checkpoint.verification_stage is VerificationStage.BEFORE_CALL
    # The execution unit has no pause/resume-from-step field; a future unit may
    # consume this artifact as input after a separate Workflow/approval boundary.
    assert ExecutionUnit.model_validate(unit_payload()).resume_strategy == "restart_unit"


@pytest.mark.parametrize(
    "change",
    [
        {"invocation_count": 2},
        {"dpabi_entrypoint": "DPARSF_run"},
        {"retry_scope": "resume_failed_node"},
        {"resume_strategy": "resume_checkpoint"},
    ],
)
def test_contract_rejects_unimplemented_execution_semantics(change: dict[str, object]) -> None:
    payload = {**unit_payload(), **change}

    with pytest.raises(ValidationError):
        ExecutionUnit.model_validate(payload)


def test_contract_requires_both_pre_and_post_call_gates() -> None:
    payload = unit_payload()
    payload["gates"] = (payload["gates"][0],)  # type: ignore[index]

    with pytest.raises(ValidationError, match="before and after"):
        ExecutionUnit.model_validate(payload)


def test_contract_does_not_allow_unverified_reusable_checkpoint() -> None:
    payload = unit_payload()
    payload["inputs"] = (
        ArtifactPort(
            artifact_ref="artifact:unverified-timeseries",
            artifact_type="preprocessing.functional_timeseries",
            verification_stage=VerificationStage.AFTER_CALL,
            reusable_checkpoint=True,
        ),
    )

    with pytest.raises(ValidationError, match="inputs must be verified before"):
        ExecutionUnit.model_validate(payload)


def test_claimed_verification_level_requires_evidence_reference() -> None:
    payload = unit_payload()
    payload["verification_maturity"] = VerificationMaturity.SYNTHETIC_MATLAB_SMOKE

    with pytest.raises(ValidationError, match="evidence references"):
        ExecutionUnit.model_validate(payload)


def test_capability_catalog_covers_every_registered_builtin_and_compute_step() -> None:
    registry = build_default_tool_registry()
    registered = set(registry._by_capability)
    classified = (
        DPARSFA_EXECUTION_UNIT_CAPABILITIES
        | PREPROCESSING_OUTSIDE_UNIT_CAPABILITIES
        | OTHER_EXECUTION_CAPABILITIES
    )
    builtin_steps = {step.capability for spec in builtin_skill_specs() for step in spec.steps}
    compute = {
        capability
        for capability, definition in registry._by_capability.items()
        if definition.risk is ToolRisk.COMPUTE
    }

    assert builtin_steps <= registered
    assert registered == classified
    assert compute == DPARSFA_COMPUTE_CAPABILITIES | OTHER_EXECUTION_CAPABILITIES


def test_dpabi_compilation_groups_logical_steps_without_changing_cfg_projection() -> None:
    manifest, manifest_content = manifest_for()
    plan = frozen_plan(manifest, parameters=common_parameters())
    without_logical_steps = compile_fixture(plan, manifest, manifest_content)
    populated_plan = plan.model_copy(
        update={
            "steps": (
                ResolvedStep(
                    step_id="preprocess_common",
                    tool=ResolvedToolRef(
                        capability="fmri.dpabi.preprocess",
                        tool_id="dpabi.preprocess",
                        version="1.0.0",
                        content_hash="a" * 64,
                    ),
                    needs=("stage_dpabi_input",),
                    consumes=("functional.staged",),
                    produces=("timeseries.unverified.unfiltered.unsmoothed",),
                    parameter_names=("preprocessing", "base_cfg_artifact_id"),
                    qc_gate=True,
                ),
                ResolvedStep(
                    step_id="verify_preprocessed_metadata",
                    tool=ResolvedToolRef(
                        capability="fmri.artifact.verify_metadata",
                        tool_id="artifact.verify_metadata",
                        version="1.0.0",
                        content_hash="b" * 64,
                    ),
                    needs=("preprocess_common",),
                    consumes=("timeseries.unverified.unfiltered.unsmoothed",),
                    produces=("timeseries.verified.unfiltered.unsmoothed",),
                    parameter_names=("preprocessing",),
                    qc_gate=True,
                ),
            )
        }
    )
    populated_plan = rehash(populated_plan)
    compiled = compile_fixture(populated_plan, manifest, manifest_content)

    assert isinstance(compiled, PreprocessingCompilation)
    assert without_logical_steps.execution_unit is None
    unit = compiled.execution_unit
    assert unit is not None
    assert unit.dpabi_entrypoint == "DPARSFA_run"
    assert unit.logical_step_ids == ("preprocess_common", "verify_preprocessed_metadata")
    assert {gate.position for gate in unit.gates} == {
        GatePosition.BEFORE_CALL,
        GatePosition.AFTER_CALL,
    }
    assert any(
        output.artifact_type == "preprocessing.functional_timeseries"
        and output.reusable_checkpoint
        and output.verification_stage is VerificationStage.AFTER_CALL
        for output in unit.outputs
    )
    assert (
        compiled.spec.payload.metric_projection.cfg_hash
        == without_logical_steps.spec.payload.metric_projection.cfg_hash
    )
    assert compiled.spec.expected_artifacts == without_logical_steps.spec.expected_artifacts
    validate_preprocessing_execution_unit(
        populated_plan,
        compiled.spec,
        compiled.execution_unit,
        approval_record_id="approval-01",
        run_id="run-01",
        job_id="job-01",
    )

    assert compiled.execution_unit is not None
    with pytest.raises(ValueError, match="missing its execution unit"):
        validate_preprocessing_execution_unit(
            populated_plan,
            compiled.spec,
            None,
            approval_record_id="approval-01",
            run_id="run-01",
            job_id="job-01",
        )

    forged = compiled.execution_unit.model_copy(update={"unit_id": "forged-unit"})
    with pytest.raises(ValueError, match="does not match"):
        validate_preprocessing_execution_unit(
            populated_plan,
            compiled.spec,
            forged,
            approval_record_id="approval-01",
            run_id="run-01",
            job_id="job-01",
        )

    changed_spec = compiled.spec.model_copy(update={"input_manifest_hash": "f" * 64})
    with pytest.raises(ValueError, match="authority does not match"):
        validate_preprocessing_execution_unit(
            populated_plan,
            changed_spec,
            compiled.execution_unit,
            approval_record_id="approval-01",
            run_id="run-01",
            job_id="job-01",
        )

    # Empty-step legacy/compiler fixtures keep their existing unclassified path.
    validate_preprocessing_execution_unit(
        plan,
        without_logical_steps.spec,
        without_logical_steps.execution_unit,
        approval_record_id="approval-01",
        run_id="run-01",
        job_id="job-01",
    )


def test_combined_alff_reho_unit_covers_compute_and_leaves_gates_outside(
    base_lineage,
) -> None:
    manifest, manifest_content = manifest_for()
    requested = request(base_lineage)
    plan = frozen_plan(
        manifest,
        parameters=common_parameters(),
        alff=requested.alff_falff,
        reho=requested.reho,
    )
    unit_capabilities = DPARSFA_EXECUTION_UNIT_CAPABILITIES
    outside_capabilities = PREPROCESSING_OUTSIDE_UNIT_CAPABILITIES
    all_capabilities = sorted(unit_capabilities | outside_capabilities)
    steps = tuple(
        ResolvedStep(
            step_id=f"logical_step_{index}",
            tool=ResolvedToolRef(
                capability=capability,
                tool_id=f"test_tool_{index}",
                version="1.0.0",
                content_hash=str(index).zfill(64),
            ),
            needs=(),
            consumes=(),
            produces=(),
            parameter_names=(),
            qc_gate=capability.startswith("fmri.qc."),
        )
        for index, capability in enumerate(all_capabilities)
    )
    plan = rehash(plan.model_copy(update={"steps": steps}))
    compiled = compile_fixture(plan, manifest, manifest_content)

    assert compiled.execution_unit is not None
    assert {step.tool.capability for step in plan.steps} & DPARSFA_COMPUTE_CAPABILITIES == (
        DPARSFA_COMPUTE_CAPABILITIES
    )
    expected_unit_step_ids = {
        step.step_id for step in plan.steps if step.tool.capability in unit_capabilities
    }
    outside_step_ids = {
        step.step_id for step in plan.steps if step.tool.capability in outside_capabilities
    }
    assert set(compiled.execution_unit.logical_step_ids) == expected_unit_step_ids
    assert not set(compiled.execution_unit.logical_step_ids) & outside_step_ids
    validate_preprocessing_capability_coverage(plan)


@pytest.mark.parametrize(
    ("capability", "message"),
    [
        (
            "fmri.dpabi.future_compute",
            "unregistered or unmapped capabilities",
        ),
        (
            "fmri.dpabi.statistics.ttest",
            "requiring a different execution unit",
        ),
    ],
)
def test_preprocessing_capability_coverage_fails_closed(capability: str, message: str) -> None:
    manifest, _ = manifest_for()
    plan = frozen_plan(manifest, parameters=common_parameters())
    step = ResolvedStep(
        step_id="unmapped_compute",
        tool=ResolvedToolRef(
            capability=capability,
            tool_id="unmapped_compute",
            version="1.0.0",
            content_hash="c" * 64,
        ),
        needs=(),
        consumes=(),
        produces=(),
        parameter_names=(),
        qc_gate=False,
    )
    plan = rehash(plan.model_copy(update={"steps": (step,)}))

    with pytest.raises(ValueError, match=message):
        validate_preprocessing_capability_coverage(plan)
