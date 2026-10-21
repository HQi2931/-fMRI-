"""Read-only contracts for persistent WorkTask automation."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from neuroagent.application.automation.models import TaskStatus, TaskStepStatus
from neuroagent.application.contracts.plans import PlanRevisionView
from neuroagent.application.contracts.shared import StrictModel
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
    step_id: str
    step_sequence: int
    step_kind: str
    step_status: TaskStepStatus
    plan_revision_id: str
    plan_revision: int
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
    step_id: str
    step_sequence: int
    plan_revision_id: str
    plan_revision: int
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
    plan_state: PlanState
    execution_authorized: Literal[False] = False
    expected_artifacts: tuple[DpabiPreprocessingOutput, ...] = ()
    plan: PlanRevisionView
