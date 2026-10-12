"""HTTP request schemas for run and QC commands.

These models intentionally do not inherit from application messages. The
router maps them into application-owned commands after transport validation.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from neuroagent.api.schemas.shared import StrictHttpModel
from neuroagent.domain.execution import (
    ExecutionBackend,
    ExecutionWorkspaceMode,
    MockOutcome,
    QcExecutionMode,
)
from neuroagent.domain.fmri.qc import HeadMotionQcCriteria, QcCheck, QcReviewScope


class RunCreate(StrictHttpModel):
    project_id: str
    plan_revision_id: str
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    max_attempts: int = Field(default=1, ge=1, le=5)
    execution_backend: ExecutionBackend = ExecutionBackend.MOCK
    workspace_mode: ExecutionWorkspaceMode = ExecutionWorkspaceMode.ISOLATED
    real_execution_confirmed: bool = False
    qc_mode: QcExecutionMode = QcExecutionMode.REQUIRE
    mock_outcome: MockOutcome = MockOutcome.SUCCEED
    mock_delay_ms: int = Field(default=0, ge=0, le=10_000)

    @model_validator(mode="after")
    def require_real_execution_confirmation(self) -> RunCreate:
        if self.execution_backend is ExecutionBackend.MATLAB and not self.real_execution_confirmed:
            raise ValueError("real MATLAB execution requires explicit confirmation")
        if self.workspace_mode is ExecutionWorkspaceMode.IN_PLACE and (
            self.execution_backend is not ExecutionBackend.MATLAB
        ):
            raise ValueError("in-place workspace mode is available only for MATLAB execution")
        return self


class RunAction(StrictHttpModel):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)


class QcReviewCreate(StrictHttpModel):
    run_id: str
    expected_run_version: int = Field(ge=1)
    review_scope: QcReviewScope = QcReviewScope.PRE_STATISTICS
    metric_artifact_ids: tuple[str, ...] = ()
    checks: tuple[QcCheck, ...] = Field(min_length=1)
    included_subject_ids: tuple[str, ...]
    excluded_subject_ids: tuple[str, ...]
    exclusion_reasons: tuple[tuple[str, str], ...]

    @model_validator(mode="after")
    def scope_matches_inputs(self) -> QcReviewCreate:
        if self.review_scope is QcReviewScope.PRE_STATISTICS and not self.metric_artifact_ids:
            raise ValueError("pre-statistics QC requires at least one metric Artifact ID")
        return self


class HeadMotionQcCreate(StrictHttpModel):
    expected_run_version: int = Field(ge=1)
    criteria: HeadMotionQcCriteria = Field(default_factory=HeadMotionQcCriteria)


class QcReviewApprove(StrictHttpModel):
    expected_review_version: int = Field(ge=1)
    expected_run_version: int = Field(ge=1)
    review_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor: str = Field(min_length=1, max_length=200)
    approved: bool
    reason: str = Field(min_length=1, max_length=2000)


class QcSkipCreate(StrictHttpModel):
    expected_run_version: int = Field(ge=1)
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)
