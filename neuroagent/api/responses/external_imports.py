"""External Imports contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from neuroagent.api.schemas.shared import StrictHttpModel as StrictModel
from neuroagent.domain.fmri.artifacts import MetricScaling
from neuroagent.domain.fmri.external_import import (
    ExternalCoordinateSpace,
    ExternalMetricInclusion,
    ExternalMetricInspection,
)
from neuroagent.domain.fmri.metrics import MetricKind


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
