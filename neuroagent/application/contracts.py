"""Stable application and API contracts for the local MVP."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from neuroagent.agent.models import (
    AgentTaskRequest,
    GatewayResult,
    ModelProfile,
    RoutingDecision,
)
from neuroagent.analysis.models import (
    AtlasPoint,
    ClusterLocalization,
    ClusterRecord,
    FailureDiagnosis,
    MlDesignRecommendation,
    MlTemplate,
    RoiExtractionRequest,
    RoiSignalRecord,
    RsFmriAnswer,
    TableInspection,
)
from neuroagent.analysis.organization import OrganizationPreview
from neuroagent.domain.fmri.metrics import AlffFalffParameters, MetricKind, RehoParameters
from neuroagent.domain.fmri.preprocessing import PreprocessingParameters
from neuroagent.domain.fmri.qc import QcCheck, QcReviewRevision
from neuroagent.domain.fmri.statistics import (
    CorrectionSpec,
    StatisticalDesignRevision,
)
from neuroagent.skills.models import SkillPlan


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorBody(StrictModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None


class ErrorResponse(StrictModel):
    error: ErrorBody


class ProjectCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    source_roots: list[str] = Field(min_length=1)
    work_root: str


class ProjectView(StrictModel):
    project_id: str
    name: str
    source_roots: list[str]
    work_root: str
    version: int
    created_at: datetime


class DatasetKind(StrEnum):
    BIDS = "bids"
    DPABI_READY = "dpabi_ready"
    DICOM = "dicom"
    NIFTI = "nifti"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class DatasetCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    source_path: str
    expected_project_version: int = Field(ge=1)


class DatasetView(StrictModel):
    dataset_id: str
    project_id: str
    name: str
    source_path: str
    version: int
    current_manifest_id: str | None = None
    created_at: datetime


class ManifestScanRequest(StrictModel):
    expected_dataset_version: int = Field(ge=1)


class SubjectManifestEntry(StrictModel):
    subject_id: str
    session_id: str | None = None
    functional_files: list[str] = Field(default_factory=list)
    anatomical_files: list[str] = Field(default_factory=list)
    dicom_files: list[str] = Field(default_factory=list)


class DatasetProfile(StrictModel):
    kind: DatasetKind
    file_count: int
    nifti_count: int
    dicom_count: int
    subject_count: int
    warnings: list[str] = Field(default_factory=list)


class ManifestRevisionView(StrictModel):
    manifest_id: str
    dataset_id: str
    revision: int
    content_hash: str
    profile: DatasetProfile
    subjects: list[SubjectManifestEntry]
    created_at: datetime


class DemographicsImportRequest(StrictModel):
    source_path: str
    subject_id_column: str
    column_mapping: dict[str, str] = Field(default_factory=dict)
    encoding: str = "utf-8-sig"
    expected_dataset_version: int = Field(ge=1)

    @field_validator("subject_id_column")
    @classmethod
    def normalize_subject_id_column(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("subject_id_column must not be blank")
        return normalized

    @field_validator("column_mapping")
    @classmethod
    def protect_canonical_subject_id(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        used_sources: set[str] = set()
        for target, source in value.items():
            normalized_target = target.strip()
            normalized_source = source.strip()
            if not normalized_target or not normalized_source:
                raise ValueError("demographics mapping names must not be blank")
            if normalized_target == "subject_id":
                raise ValueError("subject_id is reserved for the canonical identifier")
            if normalized_target in normalized:
                raise ValueError("demographics mapping targets must be unique after trimming")
            if normalized_source in used_sources:
                raise ValueError("demographics mapping sources must be unique")
            normalized[normalized_target] = normalized_source
            used_sources.add(normalized_source)
        return normalized


class DemographicsRevisionView(StrictModel):
    demographics_id: str
    dataset_id: str
    revision: int
    content_hash: str
    row_count: int
    columns: list[str]
    missing_subject_ids: list[str]
    extra_subject_ids: list[str]
    created_at: datetime


class DatasetSplitCreate(StrictModel):
    expected_dataset_version: int = Field(ge=1)
    seed: int
    train_ratio: float = Field(gt=0, lt=1)
    validation_ratio: float = Field(ge=0, lt=1)
    test_ratio: float = Field(ge=0, lt=1)
    stratify_by: str | None = None
    demographics_revision_id: str | None = None

    @model_validator(mode="after")
    def ratios_sum_to_one(self) -> DatasetSplitCreate:
        total = self.train_ratio + self.validation_ratio + self.test_ratio
        if abs(total - 1.0) > 1e-9:
            raise ValueError("train/validation/test ratios must sum to 1")
        if self.stratify_by and not self.demographics_revision_id:
            raise ValueError("stratified split requires demographics_revision_id")
        return self


class DatasetSplitView(StrictModel):
    split_id: str
    dataset_id: str
    revision: int
    content_hash: str
    seed: int
    stratify_by: str | None = None
    train_subject_ids: list[str]
    validation_subject_ids: list[str]
    test_subject_ids: list[str]
    created_at: datetime


class PlanState(StrEnum):
    DRAFT = "draft"
    VALIDATING = "validating"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    SUPERSEDED = "superseded"


class PlanRevisionCreate(StrictModel):
    project_id: str
    expected_project_version: int = Field(ge=1)
    plan: dict[str, Any]
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    supersedes_plan_revision_id: str | None = None

    @field_validator("plan")
    @classmethod
    def reject_executable_text(cls, value: dict[str, Any]) -> dict[str, Any]:
        forbidden = {"shell", "command", "matlab_script", "script_text", "code"}
        present: set[str] = set()

        def inspect(item: Any) -> None:
            if isinstance(item, dict):
                present.update(forbidden.intersection(str(key).lower() for key in item))
                for nested in item.values():
                    inspect(nested)
            elif isinstance(item, list):
                for nested in item:
                    inspect(nested)

        inspect(value)
        if present:
            raise ValueError(f"free executable text is forbidden: {sorted(present)}")
        return value


class ValidationIssue(StrictModel):
    code: str
    message: str
    severity: str = Field(pattern="^(info|warning|blocking)$")
    path: str | None = None


class PlanValidationRequest(StrictModel):
    expected_version: int = Field(ge=1)
    issues: list[ValidationIssue] = Field(default_factory=list)


class PlanRevisionView(StrictModel):
    plan_revision_id: str
    project_id: str
    revision: int
    version: int
    plan_hash: str
    manifest_hash: str
    environment_hash: str
    state: PlanState
    plan: dict[str, Any]
    validation_issues: list[ValidationIssue]
    supersedes_plan_revision_id: str | None = None
    created_at: datetime
    updated_at: datetime


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalCreate(StrictModel):
    expected_version: int = Field(ge=1)
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor: str = Field(min_length=1, max_length=200)
    decision: ApprovalDecision
    reason: str = Field(min_length=1, max_length=2000)


class ApprovalView(StrictModel):
    approval_id: str
    plan_revision_id: str
    plan_hash: str
    actor: str
    decision: ApprovalDecision
    reason: str
    created_at: datetime


class MockOutcome(StrEnum):
    SUCCEED = "succeed"
    FAIL_RETRYABLE = "fail_retryable"
    FAIL_TERMINAL = "fail_terminal"
    TIMEOUT = "timeout"


class RunCreate(StrictModel):
    project_id: str
    plan_revision_id: str
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    max_attempts: int = Field(default=1, ge=1, le=5)
    mock_outcome: MockOutcome = MockOutcome.SUCCEED
    mock_delay_ms: int = Field(default=0, ge=0, le=10_000)


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
    metric_artifact_ids: tuple[str, ...] = Field(min_length=1)
    checks: tuple[QcCheck, ...] = Field(min_length=1)
    included_subject_ids: tuple[str, ...]
    excluded_subject_ids: tuple[str, ...]
    exclusion_reasons: tuple[tuple[str, str], ...]


class QcReviewApprove(StrictModel):
    expected_review_version: int = Field(ge=1)
    expected_run_version: int = Field(ge=1)
    review_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor: str = Field(min_length=1, max_length=200)
    approved: bool
    reason: str = Field(min_length=1, max_length=2000)


class QcReviewView(StrictModel):
    review: QcReviewRevision
    run_id: str
    project_id: str
    revision: int
    version: int
    state: QcReviewState
    created_at: datetime
    updated_at: datetime


class WorkflowState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    QC_REVIEW = "qc_review"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


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


class EnvironmentComponent(StrictModel):
    name: str
    available: bool
    evidence: str | None = None


class EnvironmentProbeView(StrictModel):
    ready: bool
    environment_hash: str
    components: list[EnvironmentComponent]


class HealthView(StrictModel):
    status: str = "ok"
    database: str = "ok"


class SkillPlanIntent(StrictModel):
    """Client intent without client-authored Artifact lineage or environment locks."""

    project_id: str = Field(min_length=1)
    dataset_ref: str = Field(min_length=1)
    input_manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    requested_metrics: tuple[MetricKind, ...]
    primary_outputs: tuple[str, ...]
    input_artifact_id: str | None = None
    alff_falff: AlffFalffParameters | None = None
    reho: RehoParameters | None = None
    study_protocol_ref: str = Field(min_length=1)
    request_preprocessing: bool = False
    preprocessing: PreprocessingParameters | None = None

    @model_validator(mode="after")
    def intent_is_complete(self) -> SkillPlanIntent:
        if self.request_preprocessing:
            if self.preprocessing is None:
                raise ValueError("preprocessing parameters are required")
            if self.input_artifact_id is not None:
                raise ValueError(
                    "initial preprocessing input is derived from the frozen dataset manifest"
                )
        else:
            if self.preprocessing is not None:
                raise ValueError("preprocessing parameters require request_preprocessing=true")
            if self.input_artifact_id is None:
                raise ValueError("metric-only planning requires a registered input Artifact ID")
        return self


class SkillPlanResolveRequest(StrictModel):
    request: SkillPlanIntent
    expected_project_version: int = Field(ge=1)
    supersedes_plan_revision_id: str | None = None


class SkillPlanResolveView(StrictModel):
    skill_plan: SkillPlan
    plan_revision: PlanRevisionView


class StatisticalDesignCreate(StrictModel):
    project_id: str
    expected_project_version: int = Field(ge=1)
    input_manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    design: StatisticalDesignRevision
    correction: CorrectionSpec | None = None
    supersedes_plan_revision_id: str | None = None


class StatisticalDesignValidationRequest(StrictModel):
    expected_version: int = Field(ge=1)


class StatisticalDesignView(StrictModel):
    design: StatisticalDesignRevision
    correction: CorrectionSpec | None
    design_matrix: tuple[tuple[float, ...], ...]
    plan_revision: PlanRevisionView


class CorrectionCapabilityView(StrictModel):
    method: str
    skill_id: str
    description: str
    schema_: dict[str, Any] = Field(alias="schema", serialization_alias="schema")


class StatisticsRunCreate(StrictModel):
    project_id: str
    statistical_design_revision_id: str
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    max_attempts: int = Field(default=1, ge=1, le=5)


class StatisticalResultView(StrictModel):
    """Immutable summary of one registered statistical reproducibility report."""

    result_id: str
    project_id: str
    run_id: str
    design_revision_id: str
    mode: str
    non_scientific: bool
    non_scientific_reason: str | None
    bundle_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_count: int = Field(ge=1)
    cluster_count: int = Field(ge=0)
    version: int = Field(ge=1)
    created_at: datetime


class StatisticalResultDetailView(StatisticalResultView):
    """Full registered result including the frozen manifest and both report renderings."""

    manifest: dict[str, Any]
    report_markdown: str
    report_json: str


class ModelProfileInput(ModelProfile):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("api_key_env")
    @classmethod
    def restrict_secret_environment_name(cls, value: str) -> str:
        if not value.endswith("_API_KEY"):
            raise ValueError("api_key_env must end with _API_KEY")
        return value

    @field_validator("base_url")
    @classmethod
    def reject_secret_bearing_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain credentials, query, or fragment")
        return value


class ModelProfileView(StrictModel):
    profile: ModelProfileInput
    version: int
    created_at: datetime


class ProviderTestRequest(StrictModel):
    profile_id: str
    expected_profile_version: int = Field(ge=1)


class ProviderTestView(StrictModel):
    profile_id: str
    available: bool
    routing: RoutingDecision
    context_hash: str


class AgentTaskCreate(StrictModel):
    request: AgentTaskRequest
    expected_project_version: int = Field(ge=1)


class AgentTaskView(StrictModel):
    task_id: str
    project_id: str
    state: str
    result: GatewayResult
    created_at: datetime


class RunDiagnosisRequest(StrictModel):
    """A bounded log excerpt supplied by the UI for deterministic diagnosis."""

    log_text: str = Field(min_length=1, max_length=200_000)


class RunDiagnosisView(StrictModel):
    run_id: str | None = None
    diagnosis: FailureDiagnosis


class MlTableInspectRequest(StrictModel):
    project_id: str
    source_path: str
    max_rows: int = Field(default=100_000, ge=1, le=100_000)


class MlTableInspectView(StrictModel):
    project_id: str
    source_path_name: str
    inspection: TableInspection


class MlTemplateCreateRequest(StrictModel):
    design: MlDesignRecommendation
    source_filename: str = Field(
        default="features.csv",
        pattern=r"^[A-Za-z0-9_.-]+\.(csv|tsv|xlsx)$",
    )


class MlTemplateView(StrictModel):
    template: MlTemplate
    approval_required: bool = True


class RoiTableCreateRequest(StrictModel):
    design: RoiExtractionRequest
    records: tuple[RoiSignalRecord, ...] = Field(min_length=1, max_length=1_000_000)


class RoiTableView(StrictModel):
    valid: bool
    issues: tuple[str, ...]
    long_rows: tuple[dict[str, Any], ...] = ()
    wide_rows: tuple[dict[str, Any], ...] = ()


class ClusterLocalizationRequest(StrictModel):
    clusters: tuple[ClusterRecord, ...] = Field(min_length=1, max_length=100_000)
    atlas_points: tuple[AtlasPoint, ...] = ()
    max_distance_mm: float = Field(default=8, gt=0, le=100)


class ClusterLocalizationView(StrictModel):
    results: tuple[ClusterLocalization, ...]
    atlas_supplied: bool


class RsFmriQuestionRequest(StrictModel):
    question: str = Field(min_length=2, max_length=4_000)
    allow_remote_search: bool = False


class RsFmriAnswerView(StrictModel):
    answer: RsFmriAnswer
    remote_search_used: bool = False


class OrganizationSubjectInput(StrictModel):
    functional: tuple[str, ...] = ()
    anatomical: tuple[str, ...] = ()
    inventory: tuple[str, ...] = ()


class OrganizationPreviewRequest(StrictModel):
    project_id: str
    source_path: str
    target_stage: str = Field(pattern=r"^(FunRaw|FunImg)$")
    subjects: dict[str, OrganizationSubjectInput] = Field(min_length=1)


class OrganizationPreviewView(StrictModel):
    project_id: str
    source_path_name: str
    preview: OrganizationPreview
