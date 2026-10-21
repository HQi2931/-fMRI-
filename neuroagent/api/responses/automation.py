"""HTTP response DTOs for durable WorkTask automation."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from neuroagent.api.schemas.shared import StrictHttpModel
from neuroagent.application.automation.models import TaskStepStatus
from neuroagent.domain.fmri.artifacts import MetricScaling
from neuroagent.domain.fmri.metrics import MetricKind, MetricMaskSource
from neuroagent.domain.fmri.preprocessing import HeadMotionModel, NormalizationMode
from neuroagent.domain.fmri.statistics import (
    Centering,
    MissingValuePolicy,
    StatisticalTest,
    Tail,
)
from neuroagent.domain.lifecycle import PlanState


class PreprocessingPlanSummaryView(StrictHttpModel):
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


class StatisticalPlanSummaryView(StrictHttpModel):
    test: StatisticalTest
    group_count: int
    condition_count: int
    image_count: int
    covariate_count: int
    contrast: tuple[float, ...]
    tail: Tail
    missing_value_policy: MissingValuePolicy


class StepPlanReviewSummaryView(StrictHttpModel):
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


class StepPlanReviewListView(StrictHttpModel):
    items: tuple[StepPlanReviewSummaryView, ...]
    truncated: bool


class StepPlanReviewImageDetailView(StrictHttpModel):
    subject_id: str
    group: str | None
    condition: str | None


class StepPlanReviewCovariateValueDetailView(StrictHttpModel):
    subject_id: str
    value: float


class StepPlanReviewCovariateDetailView(StrictHttpModel):
    name: str
    centering: Centering
    values: tuple[StepPlanReviewCovariateValueDetailView, ...]


class StepPlanReviewDetailView(StrictHttpModel):
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
