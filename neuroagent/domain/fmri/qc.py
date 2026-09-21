"""Human-gated quality-control contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from neuroagent.domain.fmri.artifacts import FrozenModel


class QcSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class QcReviewScope(StrEnum):
    HEAD_MOTION = "head_motion"
    PRE_STATISTICS = "pre_statistics"


class QcCheck(FrozenModel):
    code: str = Field(min_length=1)
    severity: QcSeverity
    passed: bool
    evidence_artifact_ids: tuple[str, ...]
    message: str = Field(min_length=1)


class HeadMotionQcCriteria(FrozenModel):
    max_translation_mm: float = Field(default=2.0, gt=0)
    max_rotation_deg: float = Field(default=2.0, gt=0)
    mean_fd_power: float = Field(default=0.2, gt=0)
    fd_power_gt_0_5_fraction: float = Field(default=0.2, ge=0, le=1)


class SubjectHeadMotionQcResult(FrozenModel):
    subject_id: str = Field(min_length=1)
    passed: bool
    max_translation_mm: float = Field(ge=0)
    max_rotation_deg: float = Field(ge=0)
    mean_fd_power: float = Field(ge=0)
    fd_power_gt_0_5_count: int = Field(ge=0)
    fd_power_gt_0_5_fraction: float = Field(ge=0, le=1)
    fd_power_gt_0_2_count: int = Field(ge=0)
    fd_power_gt_0_2_fraction: float = Field(ge=0, le=1)
    fd_power_sample_count: int = Field(gt=0)
    realignment_parameter_count: int = Field(gt=0)
    dpabi_excluded_at_2mm_2deg: bool
    failed_reasons: tuple[str, ...] = ()
    evidence_relative_paths: tuple[str, ...]


class HeadMotionQcReport(FrozenModel):
    input_manifest_hash: str = Field(min_length=64, max_length=64)
    criteria: HeadMotionQcCriteria
    subjects: tuple[SubjectHeadMotionQcResult, ...]
    included_subject_ids: tuple[str, ...]
    excluded_subject_ids: tuple[str, ...]
    exclusion_reasons: tuple[tuple[str, str], ...]
    source_directory: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def report_is_complete(self) -> HeadMotionQcReport:
        subject_ids = tuple(item.subject_id for item in self.subjects)
        if len(set(subject_ids)) != len(subject_ids):
            raise ValueError("head-motion QC subjects must be unique")
        included = set(self.included_subject_ids)
        excluded = set(self.excluded_subject_ids)
        if included | excluded != set(subject_ids) or included & excluded:
            raise ValueError("head-motion QC must partition every parsed subject")
        reason_subjects = {subject_id for subject_id, _ in self.exclusion_reasons}
        if reason_subjects != excluded:
            raise ValueError("every failed head-motion subject needs one exclusion reason")
        return self


class QcReviewRevision(FrozenModel):
    review_revision_id: str = Field(min_length=1)
    review_scope: QcReviewScope = QcReviewScope.PRE_STATISTICS
    input_manifest_hash: str = Field(min_length=64, max_length=64)
    metric_artifact_ids: tuple[str, ...]
    checks: tuple[QcCheck, ...]
    included_subject_ids: tuple[str, ...]
    excluded_subject_ids: tuple[str, ...]
    exclusion_reasons: tuple[tuple[str, str], ...]
    approved: bool
    approved_by: str | None
    approval_reason: str | None
    content_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def review_is_auditable(self) -> QcReviewRevision:
        if not self.checks:
            raise ValueError("QC review requires at least one recorded automated check")
        included = set(self.included_subject_ids)
        excluded = set(self.excluded_subject_ids)
        if len(included) != len(self.included_subject_ids):
            raise ValueError("included subject IDs must be unique")
        if len(excluded) != len(self.excluded_subject_ids):
            raise ValueError("excluded subject IDs must be unique")
        if included & excluded:
            raise ValueError("a subject cannot be both included and excluded")
        reason_subjects = {subject_id for subject_id, _ in self.exclusion_reasons}
        if reason_subjects != excluded:
            raise ValueError("every excluded subject must have exactly one recorded reason")
        if len(reason_subjects) != len(self.exclusion_reasons):
            raise ValueError("excluded subject reasons must be unique")
        blocking_failures = [
            check.code
            for check in self.checks
            if check.severity is QcSeverity.BLOCKING and not check.passed
        ]
        if self.approved and blocking_failures:
            raise ValueError(f"QC cannot be approved with blocking failures: {blocking_failures}")
        if self.approved and (not self.approved_by or not self.approval_reason):
            raise ValueError("approved QC requires approver and reason")
        if not self.approved and (self.approved_by or self.approval_reason):
            raise ValueError("unapproved QC must not carry approval evidence")
        return self


def assert_statistics_ready(review: QcReviewRevision, subject_ids: tuple[str, ...]) -> None:
    if review.review_scope is not QcReviewScope.PRE_STATISTICS:
        raise ValueError("statistics require a pre-statistics QC review revision")
    if not review.approved:
        raise ValueError("statistics require an approved QC review revision")
    if tuple(subject_ids) != review.included_subject_ids:
        raise ValueError("statistical subject order must equal the frozen QC inclusion order")
