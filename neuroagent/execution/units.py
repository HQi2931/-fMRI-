"""Prototype contracts for mapping logical Skill steps to physical executions.

These models describe an execution boundary attached to the preprocessing
compiler result. They are descriptive metadata; the existing executor remains
responsible for enforcing the approval and artifact checks. A checkpoint here
means a verified artifact that a later, separate unit may reuse. It never means
DPARSFA can pause or resume inside ``DPARSFA_run``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExecutionUnitModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionUnitKind(StrEnum):
    DPARSFA_SINGLE_CALL = "dparsfa_single_call"


class GatePosition(StrEnum):
    BEFORE_CALL = "before_call"
    AFTER_CALL = "after_call"


class GateOutcome(StrEnum):
    BLOCK_DISPATCH = "block_dispatch"
    FAIL_UNIT = "fail_unit"
    REQUIRE_REVIEW = "require_review"


class VerificationMaturity(StrEnum):
    CONTRACT_ONLY = "contract_only"
    MOCK_PIPELINE = "mock_pipeline"
    SYNTHETIC_MATLAB_SMOKE = "synthetic_matlab_smoke"
    SINGLE_DATASET_SMOKE = "single_dataset_smoke"


class VerificationStage(StrEnum):
    BEFORE_CALL = "before_call"
    AFTER_CALL = "after_call"


class ArtifactPort(ExecutionUnitModel):
    """Typed artifact dependency across an execution boundary."""

    artifact_ref: str = Field(min_length=1)
    artifact_type: str = Field(min_length=1)
    verification_stage: VerificationStage
    reusable_checkpoint: bool = False
    required: bool = True


class ExecutionGate(ExecutionUnitModel):
    gate_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    position: GatePosition
    outcome: GateOutcome
    evidence_required: tuple[str, ...] = ()


class ExecutionUnit(ExecutionUnitModel):
    """A physical DPARSFA call boundary spanning one or more logical DAG steps.

    This first contract intentionally models only the existing single-call
    preprocessor. ``retry_scope`` and ``resume_strategy`` are fixed literals:
    the Worker retries the complete unit in a fresh attempt directory, and the
    unit cannot resume from an internal DAG node.
    """

    unit_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    kind: Literal[ExecutionUnitKind.DPARSFA_SINGLE_CALL]
    logical_step_ids: tuple[str, ...] = Field(min_length=1)
    dpabi_entrypoint: Literal["DPARSFA_run"] = "DPARSFA_run"
    invocation_count: Literal[1] = 1
    inputs: tuple[ArtifactPort, ...] = Field(min_length=1)
    outputs: tuple[ArtifactPort, ...] = Field(min_length=1)
    gates: tuple[ExecutionGate, ...]
    retry_owner: Literal["workflow_worker"] = "workflow_worker"
    retry_scope: Literal["whole_unit"] = "whole_unit"
    retry_workspace: Literal["fresh_attempt"] = "fresh_attempt"
    resume_strategy: Literal["restart_unit"] = "restart_unit"
    verification_maturity: VerificationMaturity
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_boundary(self) -> ExecutionUnit:
        if len(set(self.logical_step_ids)) != len(self.logical_step_ids):
            raise ValueError("logical step IDs covered by a unit must be unique")
        gate_ids = [gate.gate_id for gate in self.gates]
        if len(gate_ids) != len(set(gate_ids)):
            raise ValueError("execution gate IDs must be unique")
        positions = {gate.position for gate in self.gates}
        if positions != {GatePosition.BEFORE_CALL, GatePosition.AFTER_CALL}:
            raise ValueError("a DPARSFA unit requires gates both before and after the call")
        refs = [port.artifact_ref for port in (*self.inputs, *self.outputs)]
        if len(refs) != len(set(refs)):
            raise ValueError("artifact refs must be unique within an execution unit")
        if any(
            port.verification_stage is not VerificationStage.BEFORE_CALL for port in self.inputs
        ):
            raise ValueError("execution inputs must be verified before the DPARSFA call")
        if any(
            port.verification_stage is not VerificationStage.AFTER_CALL for port in self.outputs
        ):
            raise ValueError("execution outputs must be verified after the DPARSFA call")
        if (
            self.verification_maturity is not VerificationMaturity.CONTRACT_ONLY
            and not self.evidence_refs
        ):
            raise ValueError("verified maturity requires evidence references")
        return self


__all__ = [
    "ArtifactPort",
    "ExecutionGate",
    "ExecutionUnit",
    "ExecutionUnitKind",
    "GateOutcome",
    "GatePosition",
    "VerificationMaturity",
    "VerificationStage",
]
