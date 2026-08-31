"""Stable contracts for analysis helpers.

These models intentionally describe plans and metadata only.  They do not
contain absolute source paths, executable text, or mutable workflow state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class AnalysisModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FailureCode(StrEnum):
    INPUT_MISSING = "input_missing"
    INPUT_FORMAT = "input_format"
    NIFTI_HEADER = "nifti_header"
    DPABI_CONFIGURATION = "dpabi_configuration"
    SOFTWARE_ENVIRONMENT = "software_environment"
    RESOURCE = "resource"
    PATH_POLICY = "path_policy"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class FailureDiagnosis(AnalysisModel):
    code: FailureCode
    severity: str = Field(pattern=r"^(info|warning|error|blocking)$")
    summary: str = Field(min_length=1, max_length=500)
    evidence: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    confidence: float = Field(ge=0, le=1)
    requires_new_plan: bool
    source: str = "deterministic_log_classifier"


class RemediationProposal(AnalysisModel):
    proposal_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    changes: tuple[str, ...] = Field(min_length=1)
    risk: str = Field(pattern=r"^(low|medium|high)$")
    requires_approval: bool = True
    diagnosis_code: FailureCode


class TableColumnProfile(AnalysisModel):
    name: str = Field(min_length=1)
    kind: str = Field(pattern=r"^(numeric|categorical|text|empty)$")
    missing_count: int = Field(ge=0)
    unique_count: int = Field(ge=0)
    examples: tuple[str, ...] = ()


class TableInspection(AnalysisModel):
    filename: str = Field(min_length=1)
    format: str = Field(pattern=r"^(csv|tsv|xlsx)$")
    row_count: int = Field(ge=0)
    columns: tuple[TableColumnProfile, ...]
    duplicate_rows: int = Field(ge=0)
    target_candidates: tuple[str, ...] = ()
    subject_candidates: tuple[str, ...] = ()
    leakage_candidates: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    blocking_issues: tuple[str, ...] = ()
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class MlModelName(StrEnum):
    LOGISTIC_REGRESSION = "logistic_regression"
    SVM = "svm"
    RANDOM_FOREST = "random_forest"
    GRADIENT_BOOSTING = "gradient_boosting"


class MlDesignRecommendation(AnalysisModel):
    target_column: str = Field(min_length=1)
    group_column: str = Field(min_length=1)
    feature_columns: tuple[str, ...] = Field(min_length=1)
    models: tuple[MlModelName, ...] = Field(min_length=1)
    seed: int
    validation_strategy: str = "subject_grouped_stratified_cross_validation"
    metrics: tuple[str, ...] = ("roc_auc", "average_precision", "balanced_accuracy")
    warnings: tuple[str, ...] = ()
    requires_approval: bool = True


class MlTemplate(AnalysisModel):
    filename: str = Field(pattern=r"^[A-Za-z0-9_.-]+\.py$")
    content: str = Field(min_length=1)
    design_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class RoiSignalRecord(AnalysisModel):
    subject_id: str = Field(min_length=1)
    session_id: str | None = None
    metric: str = Field(min_length=1)
    atlas_id: str = Field(min_length=1)
    roi_index: int = Field(gt=0)
    roi_label: str = Field(min_length=1)
    value: float


class RoiExtractionRequest(AnalysisModel):
    input_artifact_id: str = Field(min_length=1)
    atlas_artifact_id: str = Field(min_length=1)
    mask_artifact_id: str | None = None
    tr_seconds: float = Field(gt=0)
    band_low_hz: float = Field(ge=0)
    band_high_hz: float = Field(gt=0)
    multiple_labels: bool
    selected_roi_indices: tuple[int, ...] = ()
    detrend: bool
    scrubbing_timing: str = Field(pattern=r"^(disabled|before_filtering|after_filtering)$")
    scrubbing_method: str | None = None
    cut_number: int = Field(default=10, ge=1, le=100)


class AtlasPoint(AnalysisModel):
    x: float
    y: float
    z: float
    label: str = Field(min_length=1)
    coordinate_space: str = Field(default="MNI", min_length=1)


class ClusterRecord(AnalysisModel):
    cluster_id: str = Field(min_length=1)
    peak_x: float
    peak_y: float
    peak_z: float
    voxel_count: int | None = Field(default=None, ge=1)
    statistic: float | None = None
    coordinate_space: str = Field(default="MNI", min_length=1)


class ClusterLocalization(AnalysisModel):
    cluster: ClusterRecord
    atlas_label: str | None = None
    distance_mm: float | None = Field(default=None, ge=0)
    confidence: float = Field(ge=0, le=1)
    limitation: str = (
        "coordinate matching is geometric nearest-neighbour in millimetres; "
        "no NIfTI grid sampling or spatial realignment is performed"
    )


class EvidenceChunk(AnalysisModel):
    source: str = Field(min_length=1)
    title: str = Field(min_length=1)
    excerpt: str = Field(min_length=1, max_length=2000)
    score: float = Field(ge=0)


class RsFmriAnswer(AnalysisModel):
    in_scope: bool
    answer: str = Field(min_length=1)
    evidence: tuple[EvidenceChunk, ...] = ()
    disclaimer: str = "研究用途信息, 不构成临床诊断或个体医疗建议。"
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
