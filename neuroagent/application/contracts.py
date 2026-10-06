"""Stable application and API contracts for the local MVP."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from ipaddress import ip_address
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

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
from neuroagent.domain.fmri.qc import (
    HeadMotionQcCriteria,
    QcCheck,
    QcReviewRevision,
    QcReviewScope,
)
from neuroagent.domain.fmri.statistics import (
    CorrectionSpec,
    StatisticalDesignRevision,
)
from neuroagent.skills.models import SkillPlan


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# A DPABI stage is a single directory name.  Keep this deliberately narrow so
# a user-selected output can never smuggle a path or traversal component into
# an organization preview.
DPABI_STAGE_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{1,63}$"


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
    report_only: bool = False
    # DPABI workspaces never infer an input stage from directory order.  A
    # non-report scan must carry the stage explicitly confirmed by the user.
    selected_input_stage: str | None = Field(default=None, min_length=1, max_length=64)


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
    issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class WorkspaceStageCandidate(StrictModel):
    stage: str = Field(min_length=1, max_length=64)
    label: str
    completed_steps: list[str] = Field(default_factory=list)
    requires_realign_parameters: bool = False
    subject_count: int = Field(ge=0)
    file_count: int = Field(ge=0)
    selectable: bool = False
    issues: list[str] = Field(default_factory=list)
    rule_version: str = Field(min_length=1, max_length=100)


class WorkspaceCheckRequest(StrictModel):
    """Read-only request to validate a user-selected DPABI workspace."""

    path: str = Field(min_length=1, max_length=4_000)
    selected_input_stage: str | None = Field(default=None, min_length=1, max_length=64)


class WorkspacePickView(StrictModel):
    path: str | None = None
    cancelled: bool = False


class WorkspaceCheckView(StrictModel):
    path: str
    kind: DatasetKind
    file_count: int
    nifti_count: int
    dicom_count: int
    subject_count: int
    functional_subject_count: int
    anatomical_subject_count: int
    input_stage: str | None = None
    selected_input_stage: str | None = None
    stage_selection_required: bool = False
    stage_candidates: list[WorkspaceStageCandidate] = Field(default_factory=list)
    output_directories: list[str] = Field(default_factory=list)
    invalid_nifti_files: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    subjects: list[SubjectManifestEntry] = Field(default_factory=list)
    total_space_bytes: int = Field(ge=0)
    free_space_bytes: int = Field(ge=0)
    checked_at: datetime


class WorkspaceSetupIntent(StrictModel):
    """Explicit metadata needed to register a checked workspace.

    The selected workspace remains the dataset source. ``work_root`` is a
    separate application-owned directory for run metadata and isolated
    attempts; it must still pass the configured path policy.
    """

    project_name: str = Field(min_length=1, max_length=200)
    dataset_name: str = Field(min_length=1, max_length=200)
    work_root: str = Field(min_length=1, max_length=4_000)
    selected_input_stage: str | None = Field(default=None, min_length=1, max_length=64)


class ManifestRevisionView(StrictModel):
    manifest_id: str
    dataset_id: str
    revision: int
    content_hash: str
    profile: DatasetProfile
    subjects: list[SubjectManifestEntry]
    selected_input_stage: str | None = None
    stage_candidates: list[WorkspaceStageCandidate] = Field(default_factory=list)
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


class ExecutionBackend(StrEnum):
    """Execution control-plane choice for a queued run."""

    MOCK = "mock"
    MATLAB = "matlab"


class ExecutionWorkspaceMode(StrEnum):
    """Where DPABI reads and writes its DataProcessDir."""

    ISOLATED = "isolated"
    IN_PLACE = "in_place"


class QcExecutionMode(StrEnum):
    """User's explicit choice for the post-processing QC gate."""

    REQUIRE = "require"
    SKIP = "skip"


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


class EnvironmentComponent(StrictModel):
    name: str
    available: bool
    evidence: str | None = None


class EnvironmentProbeView(StrictModel):
    ready: bool
    environment_hash: str
    components: list[EnvironmentComponent]


class EnvironmentConfigUpdate(StrictModel):
    """User-selected local scientific software locations.

    These values are local runtime configuration, not scientific parameters.
    The server validates the selected filesystem entries before persisting them;
    version labels are descriptive evidence and are never treated as exact
    compatibility claims.
    """

    matlab_executable: str | None = None
    spm_dir: str | None = None
    dpabi_dir: str | None = None
    matlab_version: str = Field(default="unspecified", min_length=1, max_length=120)
    spm_version: str = Field(default="unspecified", min_length=1, max_length=120)
    dpabi_version: str = Field(default="unspecified", min_length=1, max_length=120)


class EnvironmentConfigView(StrictModel):
    matlab_executable: str | None = None
    spm_dir: str | None = None
    dpabi_dir: str | None = None
    matlab_version: str
    spm_version: str
    dpabi_version: str
    configured: bool


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
    validation_mode: Literal["strict", "report_only"] = "strict"


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
    execution_backend: ExecutionBackend = ExecutionBackend.MOCK
    real_execution_confirmed: bool = False

    @model_validator(mode="after")
    def require_real_execution_confirmation(self) -> StatisticsRunCreate:
        if self.execution_backend is ExecutionBackend.MATLAB and not self.real_execution_confirmed:
            raise ValueError("real MATLAB execution requires explicit confirmation")
        return self


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


class ModelProfileCreate(StrictModel):
    model_config = ConfigDict(frozen=True)

    profile: ModelProfileInput
    api_key: str | None = None


class ModelListRequest(StrictModel):
    model_config = ConfigDict(frozen=True)

    base_url: str
    api_key: str | None = None
    api_key_env: str | None = None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlsplit(normalized)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("base_url contains an invalid port") from exc
        if port == 0:
            raise ValueError("base_url contains an invalid port")
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "base_url must be an HTTP(S) URL without credentials, query, or fragment"
            )
        if parsed.scheme == "https":
            return normalized
        hostname = parsed.hostname
        if hostname == "localhost":
            return normalized
        try:
            is_loopback = "%" not in hostname and ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = False
        if is_loopback:
            return normalized
        raise ValueError("base_url must use HTTPS or an exact HTTP loopback host")

    @field_validator("api_key_env")
    @classmethod
    def restrict_secret_environment_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.endswith("_API_KEY"):
            raise ValueError("api_key_env must end with _API_KEY")
        return value


class ModelListView(StrictModel):
    models: list[str]


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


class ConversationMode(StrEnum):
    CHAT = "chat"
    WORK = "work"


class ConversationRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ConversationToolStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    AWAITING_CONFIRMATION = "awaiting_confirmation"


class ConversationAction(StrEnum):
    AUTO = "auto"
    SETUP_WORKSPACE = "setup_workspace"
    PREPARE_PREPROCESSING_PLAN = "prepare_preprocessing_plan"
    PREVIEW_PREPROCESSING_RUN = "preview_preprocessing_run"
    START_PREPROCESSING = "start_preprocessing"
    CREATE_HEAD_MOTION_QC = "create_head_motion_qc"
    GET_PROGRESS = "get_progress"
    GET_QC_STATUS = "get_qc_status"
    APPROVE_QC_REVIEW = "approve_qc_review"
    SKIP_QC = "skip_qc"
    GET_STATISTICAL_RESULTS = "get_statistical_results"
    PROPOSE_WORKSPACE_CHANGES = "propose_workspace_changes"


class MemoryScope(StrEnum):
    CONVERSATION = "conversation"
    PROJECT = "project"


class MemoryKind(StrEnum):
    PREFERENCE = "preference"
    INSTRUCTION = "instruction"
    SCIENTIFIC_PARAMETER = "scientific_parameter"
    PROJECT_FACT = "project_fact"
    DECISION = "decision"


class MemoryStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    FORGOTTEN = "forgotten"


class MemoryAction(StrEnum):
    CONFIRM = "confirm"
    UPDATE = "update"
    REJECT = "reject"
    PIN = "pin"
    UNPIN = "unpin"
    FORGET = "forget"
    ACCEPT_PROPOSAL = "accept_proposal"
    MERGE_PROPOSAL = "merge_proposal"
    REJECT_PROPOSAL = "reject_proposal"


class ConversationCreate(StrictModel):
    mode: ConversationMode
    project_id: str | None = None
    title: str | None = Field(default=None, max_length=200)
    workspace_path: str | None = Field(default=None, max_length=4_000)
    preferred_profile_id: str | None = Field(default=None, max_length=63)


class WorkspaceFileScope(StrictModel):
    """Optional user-selected file scope for the Work file assistant."""

    paths: tuple[str, ...] = Field(default=(), max_length=200)

    @field_validator("paths")
    @classmethod
    def relative_paths_only(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for path in value:
            if (
                not path
                or path.startswith(("/", "\\"))
                or ":" in path
                or "\\" in path
                or any(part in {"", ".", ".."} for part in path.split("/"))
            ):
                raise ValueError("workspace scope paths must be relative POSIX paths")
        return value


class WorkspaceCitation(StrictModel):
    """A location in a workspace file that can be independently re-read."""

    path: str = Field(min_length=1, max_length=4_000)
    locator: str = Field(min_length=1, max_length=200)
    quote: str | None = Field(default=None, max_length=500)


class WorkspaceContentConsent(StrictModel):
    consent_id: str
    conversation_id: str
    workspace_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_id: str = Field(min_length=1, max_length=63)
    profile_revision: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    model: str = Field(min_length=1, max_length=200)
    policy_version: str
    provider_host: str
    granted_at: datetime
    revoked_at: datetime | None = None
    valid_for_current_context: bool = False


class WorkspaceContentConsentCreate(StrictModel):
    profile_id: str = Field(min_length=1, max_length=63)
    model: str = Field(min_length=1, max_length=200)
    acknowledge_remote_processing: bool = False


class WorkspaceEdit(StrictModel):
    path: str = Field(min_length=1, max_length=4_000)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    replacement: str = Field(max_length=1_048_576)
    after_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_edit(self) -> WorkspaceEdit:
        if self.expected_sha256 is None:
            if self.start_line is not None or self.end_line is not None:
                raise ValueError("new files cannot specify line ranges")
            return self
        if self.start_line is None or self.end_line is None or self.end_line < self.start_line:
            raise ValueError("existing file edits require a valid line range")
        return self


class WorkspaceChangeProposal(StrictModel):
    proposal_id: str
    conversation_id: str
    workspace_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    edits: tuple[WorkspaceEdit, ...] = Field(max_length=200)
    diff: str = Field(max_length=1_048_576)
    citations: tuple[WorkspaceCitation, ...] = ()
    status: str
    expires_at: datetime
    applied_at: datetime | None = None
    undone_at: datetime | None = None


class WorkspacePatchApplyRequest(StrictModel):
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class WorkspacePatchApplyView(StrictModel):
    proposal_id: str
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: str
    applied_at: datetime | None = None
    undo_expires_at: datetime | None = None


class WorkspacePatchUndoView(StrictModel):
    proposal_id: str
    status: str
    undone_at: datetime | None = None


class ConversationMessageView(StrictModel):
    message_id: str
    conversation_id: str
    sequence: int
    role: ConversationRole
    content: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ConversationToolCallView(StrictModel):
    tool_call_id: str
    conversation_id: str
    user_message_id: str
    tool_name: str
    status: ConversationToolStatus
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    # Agent tool-loop telemetry.  ``None`` for single-shot tool calls and for
    # rows persisted before the loop existed.
    step_index: int | None = Field(default=None, ge=1)
    risk_tier: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    created_at: datetime
    updated_at: datetime


class ConfirmationChallengeView(StrictModel):
    """One-time server-issued authorization for a real Work execution."""

    confirmation_id: str
    conversation_id: str
    project_id: str
    plan_revision_id: str
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    qc_mode: QcExecutionMode
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    consumed_run_id: str | None = None


class ConversationView(StrictModel):
    conversation_id: str
    mode: ConversationMode
    title: str
    workspace_path: str | None = None
    preferred_profile_id: str | None = None
    project_id: str | None = None
    active_run_id: str | None = None
    version: int
    created_at: datetime
    updated_at: datetime
    messages: list[ConversationMessageView] = Field(default_factory=list)
    tool_calls: list[ConversationToolCallView] = Field(default_factory=list)


class WorkDraftUpdate(StrictModel):
    base_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    patch: dict[str, Any] = Field(max_length=32)

    @field_validator("patch")
    @classmethod
    def bounded_patch(cls, value: dict[str, Any]) -> dict[str, Any]:
        import json

        if len(json.dumps(value, ensure_ascii=False, allow_nan=False)) > 64_000:
            raise ValueError("draft patch exceeds size limit")
        return value


class ConversationTurnCreate(StrictModel):
    paper_ids: tuple[str, ...] = ()
    content: str = Field(min_length=1, max_length=8_000, pattern=r"\S")
    stream: bool = False
    action: ConversationAction = ConversationAction.AUTO
    allow_remote_search: bool = False
    model: str | None = Field(default=None, min_length=1, max_length=200)
    workspace_path: str | None = Field(default=None, max_length=4_000)
    preferred_profile_id: str | None = Field(default=None, max_length=63)
    project_id: str | None = None
    target_run_id: str | None = None
    qc_review_id: str | None = None
    workspace_setup: WorkspaceSetupIntent | None = None
    skill_plan_intent: SkillPlanIntent | None = None
    expected_project_version: int | None = Field(default=None, ge=1)
    plan_revision_id: str | None = None
    expected_plan_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    real_execution_confirmed: bool = False
    qc_mode: QcExecutionMode | None = None
    confirmation_id: str | None = Field(default=None, min_length=1, max_length=64)
    workspace_file_scope: WorkspaceFileScope | None = None
    # Set only by an explicit user/UI confirmation; model output is ignored.
    selected_input_stage: str | None = Field(default=None, min_length=1, max_length=64)


class ConversationTurnView(StrictModel):
    conversation: ConversationView
    user_message: ConversationMessageView
    assistant_message: ConversationMessageView
    tool_call: ConversationToolCallView | None = None
    #: Ordered steps of a bounded Agent tool loop.  Empty for single-shot turns,
    #: where ``tool_call`` carries the one call.
    tool_calls: list[ConversationToolCallView] = Field(default_factory=list)


class MemoryCreate(StrictModel):
    importance: float = Field(default=0.5, ge=0, le=1)
    expires_at: AwareDatetime | None = None
    kind: MemoryKind
    key: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=4_000)
    scope: MemoryScope = MemoryScope.CONVERSATION
    pinned: bool = True
    source_message_id: str | None = None


class MemoryUpdate(StrictModel):
    importance: float | None = Field(default=None, ge=0, le=1)
    expires_at: AwareDatetime | None = None
    action: MemoryAction
    expected_version: int = Field(ge=1)
    content: str | None = Field(default=None, min_length=1, max_length=4_000)


class MemoryView(StrictModel):
    semantic_indexed: bool = False
    importance: float = Field(default=0.5, ge=0, le=1)
    expires_at: datetime | None = None
    proposed_content: str | None = None
    proposal_source_message_id: str | None = None
    memory_id: str
    conversation_id: str
    project_id: str | None = None
    scope: MemoryScope
    kind: MemoryKind
    key: str
    content: str
    status: MemoryStatus
    pinned: bool
    source_message_id: str | None = None
    confidence: float = Field(ge=0, le=1)
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class ContextSummaryView(StrictModel):
    summary_id: str
    conversation_id: str
    content: str
    covered_sequence: int = Field(ge=0)
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    method: str
    created_at: datetime


class ConversationContextView(StrictModel):
    summary: ContextSummaryView | None = None
    memories: list[MemoryView] = Field(default_factory=list)


class OrganizationSubjectInput(StrictModel):
    functional: tuple[str, ...] = ()
    anatomical: tuple[str, ...] = ()
    inventory: tuple[str, ...] = ()


class OrganizationPreviewRequest(StrictModel):
    project_id: str
    source_path: str
    target_stage: str = Field(
        min_length=2,
        max_length=64,
        pattern=DPABI_STAGE_PATTERN,
    )
    subjects: dict[str, OrganizationSubjectInput] = Field(min_length=1)


class OrganizationPreviewView(StrictModel):
    project_id: str
    source_path_name: str
    preview: OrganizationPreview
