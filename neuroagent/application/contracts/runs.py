"""Runs contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from neuroagent.domain.execution import (
    ExecutionBackend as ExecutionBackend,
)
from neuroagent.domain.execution import (
    ExecutionWorkspaceMode as ExecutionWorkspaceMode,
)
from neuroagent.domain.execution import (
    MockOutcome as MockOutcome,
)
from neuroagent.domain.execution import (
    QcExecutionMode as QcExecutionMode,
)
from neuroagent.domain.fmri.qc import HeadMotionQcCriteria, QcCheck, QcReviewRevision, QcReviewScope
from neuroagent.domain.lifecycle import WorkflowState

from .shared import StrictModel


class RunCreate(StrictModel):
    project_id: str
    plan_revision_id: str
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    max_attempts: int = Field(default=1, ge=1, le=5)
    execution_backend: ExecutionBackend = ExecutionBackend.MOCK
    workspace_mode: ExecutionWorkspaceMode = ExecutionWorkspaceMode.ISOLATED
    real_execution_confirmed: bool = False
    # Existing API clients retain the historical behavior; Work itself requires
    # an explicit choice before it constructs this request.
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


class RunAction(StrictModel):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)


class QcReviewState(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


class QcReviewCreate(StrictModel):
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


class HeadMotionQcCreate(StrictModel):
    expected_run_version: int = Field(ge=1)
    criteria: HeadMotionQcCriteria = Field(default_factory=HeadMotionQcCriteria)


class QcReviewApprove(StrictModel):
    expected_review_version: int = Field(ge=1)
    expected_run_version: int = Field(ge=1)
    review_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor: str = Field(min_length=1, max_length=200)
    approved: bool
    reason: str = Field(min_length=1, max_length=2000)


class QcSkipCreate(StrictModel):
    expected_run_version: int = Field(ge=1)
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)


class QcSkipView(StrictModel):
    qc_skip_id: str
    run_id: str
    project_id: str
    run_version: int = Field(ge=1)
    decision_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor: str
    reason: str
    qc_review_revision_id: str | None = None
    created_at: datetime


class QcReviewView(StrictModel):
    review: QcReviewRevision
    run_id: str
    project_id: str
    revision: int
    version: int
    state: QcReviewState
    created_at: datetime
    updated_at: datetime


class RunView(StrictModel):
    run_id: str
    project_id: str
    plan_revision_id: str
    state: WorkflowState
    version: int
    attempt: int
    cancel_requested: bool
    error: str | None = None
    stage: str = "queued"
    stage_progress: float | None = Field(default=None, ge=0, le=1)
    heartbeat: datetime | None = None
    log_cursor: int = Field(default=0, ge=0)
    eta_seconds: int | None = Field(default=None, ge=0)
    diagnosis_available: bool = False
    qc_mode: QcExecutionMode = QcExecutionMode.REQUIRE
    qc_status: str = "pending"
    created_at: datetime
    updated_at: datetime


class ArtifactView(StrictModel):
    artifact_id: str
    project_id: str
    run_id: str
    artifact_type: str
    relative_path: str
    checksum: str
    size_bytes: int
    provenance: dict[str, Any]
    created_at: datetime


class RuntimeEventView(StrictModel):
    event_id: int
    trace_id: str
    project_id: str | None = None
    run_id: str | None = None
    event_type: str
    severity: str
    payload: dict[str, Any]
    created_at: datetime
