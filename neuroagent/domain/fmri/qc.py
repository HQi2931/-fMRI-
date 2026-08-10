"""Human-gated quality-control contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from neuroagent.domain.fmri.artifacts import FrozenModel


class QcSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class QcCheck(FrozenModel):
    code: str = Field(min_length=1)
    severity: QcSeverity
    passed: bool
    evidence_artifact_ids: tuple[str, ...]
    message: str = Field(min_length=1)


class QcReviewRevision(FrozenModel):
    review_revision_id: str = Field(min_length=1)
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
    if not review.approved:
        raise ValueError("statistics require an approved QC review revision")
    if tuple(subject_ids) != review.included_subject_ids:
        raise ValueError("statistical subject order must equal the frozen QC inclusion order")
