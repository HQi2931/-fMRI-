"""HTTP commands for explicitly reviewing and starting a WorkTask step."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from neuroagent.api.responses.plans import ApprovalView
from neuroagent.api.responses.runs import RunView
from neuroagent.api.schemas.shared import StrictHttpModel
from neuroagent.domain.execution import (
    ExecutionBackend,
    ExecutionWorkspaceMode,
    MockOutcome,
    QcExecutionMode,
)


class StepPlanActionIdentity(StrictHttpModel):
    task_version: int = Field(ge=1)
    step_version: int = Field(ge=1)
    plan_revision_id: str = Field(min_length=1, max_length=100)
    plan_version: int = Field(ge=1)
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class StepPlanApproveCreate(StepPlanActionIdentity):
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)


class StepRunAuthorizationCreate(StepPlanActionIdentity):
    execution_backend: ExecutionBackend
    workspace_mode: ExecutionWorkspaceMode = ExecutionWorkspaceMode.ISOLATED
    real_execution_confirmed: bool = False
    qc_mode: QcExecutionMode = QcExecutionMode.REQUIRE

    @model_validator(mode="after")
    def require_explicit_real_execution(self) -> StepRunAuthorizationCreate:
        if self.execution_backend is ExecutionBackend.MATLAB and not self.real_execution_confirmed:
            raise ValueError("real MATLAB execution requires explicit confirmation")
        if self.workspace_mode is ExecutionWorkspaceMode.IN_PLACE and (
            self.execution_backend is not ExecutionBackend.MATLAB
        ):
            raise ValueError("in-place workspace mode is available only for MATLAB execution")
        return self


class StepRunStartCreate(StepRunAuthorizationCreate):
    confirmation_id: str = Field(min_length=1, max_length=64)
    max_attempts: int = Field(default=1, ge=1, le=5)
    mock_outcome: MockOutcome = MockOutcome.SUCCEED
    mock_delay_ms: int = Field(default=0, ge=0, le=10_000)

    @model_validator(mode="after")
    def restrict_mock_options_to_mock_backend(self) -> StepRunStartCreate:
        if self.execution_backend is not ExecutionBackend.MOCK and (
            self.mock_outcome is not MockOutcome.SUCCEED or self.mock_delay_ms != 0
        ):
            raise ValueError("mock run options are only available with the mock backend")
        return self


class StepRunAuthorizationView(StrictHttpModel):
    conversation_id: str
    project_id: str
    task_id: str
    step_id: str
    plan_revision_id: str
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmation_id: str
    expires_at: datetime


class StepPlanApproveView(StrictHttpModel):
    conversation_id: str
    project_id: str
    task_id: str
    step_id: str
    plan_revision_id: str
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    approval: ApprovalView


class StepRunStartView(StrictHttpModel):
    conversation_id: str
    project_id: str
    task_id: str
    step_id: str
    plan_revision_id: str
    run: RunView
