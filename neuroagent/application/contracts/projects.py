"""Projects contracts."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator, model_validator

from neuroagent.analysis.organization import OrganizationPreview
from neuroagent.domain.fmri.dataset import DatasetKind

from .shared import StrictModel

DPABI_STAGE_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{1,63}$"


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


class ExternalDemographicsImportRequest(StrictModel):
    source_path: str
    subject_id_column: str
    column_mapping: dict[str, str] = Field(default_factory=dict)
    encoding: str = "utf-8-sig"
    expected_import_version: int = Field(ge=1)

    @field_validator("subject_id_column")
    @classmethod
    def normalize_id_column(cls, value: str) -> str:
        return DemographicsImportRequest.normalize_subject_id_column(value)

    @field_validator("column_mapping")
    @classmethod
    def normalize_mapping(cls, value: dict[str, str]) -> dict[str, str]:
        return DemographicsImportRequest.protect_canonical_subject_id(value)


class ExternalDemographicsRevisionView(StrictModel):
    demographics_id: str
    external_import_id: str
    inclusion_hash: str
    revision: int
    content_hash: str
    source_sha256: str
    row_count: int
    columns: list[str]
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
