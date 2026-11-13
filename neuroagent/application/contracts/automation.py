"""Read-only contracts for persistent WorkTask automation."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from neuroagent.application.automation.models import TaskStatus, TaskStepStatus
from neuroagent.application.contracts.conversations import ConfirmationChallengeView
from neuroagent.application.contracts.plans import ApprovalView, PlanRevisionView
from neuroagent.application.contracts.runs import RunView
from neuroagent.application.contracts.shared import StrictModel
from neuroagent.domain.execution import (
    ExecutionBackend,
    ExecutionWorkspaceMode,
    MockOutcome,
    QcExecutionMode,
)
from neuroagent.domain.fmri.artifacts import MetricScaling
from neuroagent.domain.fmri.metrics import MetricKind, MetricMaskSource
from neuroagent.domain.fmri.preprocessing import HeadMotionModel, NormalizationMode
from neuroagent.domain.fmri.preprocessing_outputs import DpabiPreprocessingOutput
from neuroagent.domain.fmri.statistics import (
    Centering,
    MissingValuePolicy,
    StatisticalTest,
    Tail,
)
from neuroagent.domain.lifecycle import PlanState


class PreprocessingPlanSummaryView(StrictModel):
    """Privacy-limited preprocessing choices for a pending step review."""

    tr_seconds: float
    expected_time_points: int | None
    dummy_scans: int
    detrend: bool
    slice_timing_enabled: bool
    slice_count: int | None
    reference_slice: int | None
    realignment_enabled: bool
    head_motion_model: HeadMotionModel | None
    white_matter_regression_enabled: bool
    csf_regression_enabled: bool
    global_signal_regression_enabled: bool
    normalization_mode: NormalizationMode
    requested_metrics: tuple[MetricKind, ...]
    frequency_low_hz: float | None
    frequency_high_hz: float | None
    mask_source: MetricMaskSource | None
    requested_scalings: tuple[MetricScaling, ...]
    metric_smoothing: bool


class StatisticalPlanSummaryView(StrictModel):
    """Counts and test choices without exposing subject or group identities."""

    test: StatisticalTest
    group_count: int
    condition_count: int
    image_count: int
    covariate_count: int
    contrast: tuple[float, ...]
    tail: Tail
    missing_value_policy: MissingValuePolicy


class StepPlanReviewSummaryView(StrictModel):
    """Privacy-minimal summary of one currently pending Plan review."""

    conversation_id: str
    project_id: str
    task_id: str
    task_version: int = Field(ge=1)
    step_id: str
    step_sequence: int
    step_version: int = Field(ge=1)
    step_kind: str
    step_status: TaskStepStatus
    plan_revision_id: str
    plan_revision: int
    plan_version: int = Field(ge=1)
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_state: PlanState
    execution_authorized: Literal[False] = False
    expected_output_count: int
    preprocessing: PreprocessingPlanSummaryView | None
    statistical: StatisticalPlanSummaryView | None


class StepPlanReviewListView(StrictModel):
    """Bounded list of pending Plan review summaries."""

    items: tuple[StepPlanReviewSummaryView, ...]
    truncated: bool


class StepPlanReviewImageDetailView(StrictModel):
    subject_id: str
    group: str | None
    condition: str | None


class StepPlanReviewCovariateValueDetailView(StrictModel):
    subject_id: str
    value: float


class StepPlanReviewCovariateDetailView(StrictModel):
    name: str
    centering: Centering
    values: tuple[StepPlanReviewCovariateValueDetailView, ...]


class StepPlanReviewDetailView(StrictModel):
    """On-demand statistical inclusion detail with only table-facing fields."""

    conversation_id: str
    project_id: str
    task_id: str
    task_version: int = Field(ge=1)
    step_id: str
    step_sequence: int
    step_version: int = Field(ge=1)
    plan_revision_id: str
    plan_revision: int
    plan_version: int = Field(ge=1)
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    images: tuple[StepPlanReviewImageDetailView, ...]
    covariates: tuple[StepPlanReviewCovariateDetailView, ...]


class VerifiedStepPlanReviewSnapshot(StrictModel):
    """Internal verifier result; this full Plan snapshot must never be an HTTP response."""

    conversation_id: str
    project_id: str
    task_id: str
    task_version: int
    task_status: TaskStatus
    step_id: str
    step_sequence: int
    step_kind: str
    step_status: TaskStepStatus
    step_version: int
    step_input_version: str | None
    input_revision: str
    waiting_reference: str
    plan_revision_id: str
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_version: int = Field(ge=1)
    plan_state: PlanState
    execution_authorized: Literal[False] = False
    expected_artifacts: tuple[DpabiPreprocessingOutput, ...] = ()
    plan: PlanRevisionView


class StepPlanApproveView(StrictModel):
    conversation_id: str
    project_id: str
    task_id: str
    step_id: str
    plan_revision_id: str
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    approval: ApprovalView


class StepPlanActionIdentity(StrictModel):
    """Optimistic identity for a mutation of the exact reviewed TaskStep."""

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


class StepRunStartView(StrictModel):
    conversation_id: str
    project_id: str
    task_id: str
    step_id: str
    plan_revision_id: str
    run: RunView


class StepRunAuthorizationView(StrictModel):
    conversation_id: str
    project_id: str
    task_id: str
    step_id: str
    plan_revision_id: str
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    authorization: ConfirmationChallengeView
