"""Transport request schemas for projects and dataset commands."""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from neuroagent.api.schemas.shared import StrictHttpModel

DPABI_STAGE_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{1,63}$"


class ProjectCreate(StrictHttpModel):
    name: str = Field(min_length=1, max_length=200)
    source_roots: list[str] = Field(min_length=1)
    work_root: str


class DatasetCreate(StrictHttpModel):
    name: str = Field(min_length=1, max_length=200)
    source_path: str
    expected_project_version: int = Field(ge=1)


class ManifestScanRequest(StrictHttpModel):
    expected_dataset_version: int = Field(ge=1)
    report_only: bool = False
    selected_input_stage: str | None = Field(default=None, min_length=1, max_length=64)


class WorkspaceCheckRequest(StrictHttpModel):
    """Read-only request to validate a user-selected DPABI workspace."""

    path: str = Field(min_length=1, max_length=4_000)
    selected_input_stage: str | None = Field(default=None, min_length=1, max_length=64)


class WorkspaceSetupIntent(StrictHttpModel):
    """Explicit metadata needed to register a checked workspace.

    The selected workspace remains the dataset source. ``work_root`` is a
    separate application-owned directory for run metadata and isolated
    attempts; it must still pass the configured path policy.
    """

    project_name: str = Field(min_length=1, max_length=200)
    dataset_name: str = Field(min_length=1, max_length=200)
    work_root: str = Field(min_length=1, max_length=4_000)
    selected_input_stage: str | None = Field(default=None, min_length=1, max_length=64)


class DemographicsImportRequest(StrictHttpModel):
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


class ExternalDemographicsImportRequest(StrictHttpModel):
    source_path: str
    subject_id_column: str
    column_mapping: dict[str, str] = Field(default_factory=dict)
    encoding: str = "utf-8-sig"
    expected_import_version: int = Field(ge=1)

    @field_validator("subject_id_column")
    @classmethod
    def normalize_id_column(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("subject_id_column must not be blank")
        return normalized

    @field_validator("column_mapping")
    @classmethod
    def normalize_mapping(cls, value: dict[str, str]) -> dict[str, str]:
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


class DatasetSplitCreate(StrictHttpModel):
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


class OrganizationSubjectInput(StrictHttpModel):
    functional: tuple[str, ...] = ()
    anatomical: tuple[str, ...] = ()
    inventory: tuple[str, ...] = ()


class OrganizationPreviewRequest(StrictHttpModel):
    project_id: str
    source_path: str
    target_stage: str = Field(min_length=2, max_length=64, pattern=DPABI_STAGE_PATTERN)
    subjects: dict[str, OrganizationSubjectInput] = Field(min_length=1)
