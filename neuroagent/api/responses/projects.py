"""Projects contracts."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from neuroagent.analysis.organization import OrganizationPreview
from neuroagent.api.schemas.shared import StrictHttpModel as StrictModel
from neuroagent.domain.fmri.dataset import DatasetKind

DPABI_STAGE_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{1,63}$"


class ProjectView(StrictModel):
    project_id: str
    name: str
    source_roots: list[str]
    work_root: str
    version: int
    created_at: datetime


class DatasetView(StrictModel):
    dataset_id: str
    project_id: str
    name: str
    source_path: str
    version: int
    current_manifest_id: str | None = None
    created_at: datetime


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


class OrganizationPreviewView(StrictModel):
    project_id: str
    source_path_name: str
    preview: OrganizationPreview
