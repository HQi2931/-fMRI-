"""External Imports contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from neuroagent.domain.fmri.artifacts import MetricScaling
from neuroagent.domain.fmri.external_import import (
    ExternalCoordinateSpace,
    ExternalMetricInclusion,
    ExternalMetricInspection,
)
from neuroagent.domain.fmri.metrics import MetricKind

from .shared import StrictModel


class ExternalAnalysisImportInspectRequest(StrictModel):
    """Inspect a user-selected external DPABI metric directory without inclusion."""

    project_id: str = Field(min_length=1)
    source_path: str = Field(min_length=1, max_length=4096)
    layout: Literal["grouped", "paired_conditions"] = "grouped"


class ExternalAnalysisImportIncludeRequest(StrictModel):
    """Explicit human decision to make an inspected import selectable for analysis."""

    expected_version: int = Field(ge=1)
    included_subject_ids: tuple[str, ...] = Field(min_length=2)
    group_order: tuple[str, ...] = Field(min_length=2)
    coordinate_space: ExternalCoordinateSpace
    acknowledge_unknown_processing: Literal[True]
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)
    mask_relative_path: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def include_lists_are_unambiguous(self) -> ExternalAnalysisImportIncludeRequest:
        if len(set(self.included_subject_ids)) != len(self.included_subject_ids):
            raise ValueError("included_subject_ids must be unique")
        if len(set(self.group_order)) != len(self.group_order):
            raise ValueError("group_order must be unique")
        return self


class ExternalAnalysisSelectionImageView(StrictModel):
    subject_id: str
    group: str
    condition: str | None = None
    artifact_id: str


class ExternalAnalysisSelectionView(StrictModel):
    """Opaque IDs and manifest needed to freeze a reviewed external design."""

    input_manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    mask_artifact_id: str
    images: tuple[ExternalAnalysisSelectionImageView, ...]
    group_order: tuple[str, ...]


class ExternalAnalysisImportView(StrictModel):
    import_id: str
    project_id: str
    source_path: str
    status: Literal["needs_review", "included"]
    version: int = Field(ge=1)
    inspection: ExternalMetricInspection
    inclusion: ExternalMetricInclusion | None = None
    inclusion_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    selection: ExternalAnalysisSelectionView | None = None
    created_at: datetime
    updated_at: datetime


class ExternalAnalysisImportSummaryView(StrictModel):
    """Bounded list projection that omits per-subject paths and file hashes."""

    import_id: str
    layout: Literal["grouped", "paired_conditions"] = "grouped"
    project_id: str
    status: Literal["needs_review", "included"]
    version: int = Field(ge=1)
    source_tree_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    metric_kind: MetricKind | None = None
    scaling: MetricScaling | None = None
    subject_count: int = Field(ge=0)
    group_counts: dict[str, int]
    grid_signature: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    coordinate_space: ExternalCoordinateSpace
    blocking_issues: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    unknown_processing_fields: tuple[str, ...] = ()
    inclusion_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    created_at: datetime
    updated_at: datetime
