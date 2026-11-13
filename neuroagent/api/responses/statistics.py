"""Statistics contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from neuroagent.api.schemas.shared import StrictHttpModel as StrictModel
from neuroagent.domain.execution import ExecutionBackend as ExecutionBackend
from neuroagent.domain.fmri.results import StatisticalAtlasSelection
from neuroagent.domain.fmri.statistics import CorrectionSpec, StatisticalDesignRevision

from .plans import PlanRevisionView


class StatisticalDesignView(StrictModel):
    design: StatisticalDesignRevision
    localization_plan: StatisticalAtlasSelection | None = None
    correction: CorrectionSpec | None
    design_matrix: tuple[tuple[float, ...], ...]
    plan_revision: PlanRevisionView


class CorrectionCapabilityView(StrictModel):
    method: str
    skill_id: str
    description: str
    schema_: dict[str, Any] = Field(alias="schema", serialization_alias="schema")


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


class StatisticalCovariateSummary(StrictModel):
    name: str
    centering: str


class StatisticalMethodSummary(StrictModel):
    """Path-free method facts, derived from a verified frozen result."""

    test: str
    test_label: str
    function_name: str
    statistic_type: Literal["T", "R", "F"]
    subject_count: int = Field(gt=0)
    image_count: int = Field(gt=0)
    group_counts: dict[str, int]
    condition_order: tuple[str, ...]
    covariates: tuple[StatisticalCovariateSummary, ...]
    contrast: tuple[float, ...]
    tail: str
    tail_label: str
    df1: float = Field(gt=0, allow_inf_nan=False)
    df2: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    positive_direction: str | None = None
    negative_direction: str | None = None
    assumptions: tuple[str, ...]
    correction_method: Literal["fdr", "grf"] | None = None
    q_threshold: float | None = Field(default=None, gt=0, lt=1)
    actual_p_threshold: float | None = Field(default=None, ge=0, le=1)
    voxel_p_threshold: float | None = Field(default=None, gt=0, lt=1)
    cluster_p_threshold: float | None = Field(default=None, gt=0, lt=1)
    smoothness_mode: str | None = None
    smoothness_source: Literal["provided", "header", "estimated"] | None = None
    effective_smoothness_dlh: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    effective_fwhm: tuple[float, float, float] | None = None
    effective_mask_voxel_count: int | None = Field(default=None, gt=0)
    minimum_cluster_size: int | None = Field(default=None, ge=0)
    cluster_connectivity: Literal[26] | None = None
    correction_evidence_status: Literal[
        "verified", "not_registered", "unavailable", "not_applicable"
    ]
    software_versions: dict[str, str]
    evidence_issues: tuple[str, ...] = ()


class StatisticalResultDetailView(StatisticalResultView):
    """Full registered result including the frozen manifest and both report renderings."""

    manifest: dict[str, Any]
    report_markdown: str
    report_json: str
    method_summary: StatisticalMethodSummary | None = None


class StatisticalResultClusterSliceView(StrictModel):
    cluster_id: str
    slice_index: int = Field(ge=0)


class StatisticalClusterRoiRow(StrictModel):
    subject_id: str
    group: str | None = None
    condition: str | None = None
    mean_value: float = Field(allow_inf_nan=False)


class StatisticalClusterRoiView(StrictModel):
    result_id: str
    result_bundle_hash: str
    cluster_id: str | None
    status: Literal["descriptive", "no_significant_voxels", "no_reportable_clusters"]
    source_map_artifact_id: str | None = None
    voxel_count: int = Field(ge=0)
    rows: tuple[StatisticalClusterRoiRow, ...]
    limitation: str = (
        "显著簇内均值仅用于描述性展示；同一数据选择的 ROI 不构成独立验证，不提供二次显著性检验。"  # noqa: RUF001
    )


class StatisticalResultSliceInfoView(StrictModel):
    result_id: str
    map_artifact_id: str
    slice_count: int = Field(gt=0)
    initial_slice_index: int = Field(ge=0)
    cluster_slices: tuple[StatisticalResultClusterSliceView, ...]

    @model_validator(mode="after")
    def slices_fit_image(self) -> StatisticalResultSliceInfoView:
        if self.initial_slice_index >= self.slice_count:
            raise ValueError("initial axial slice must be inside the image")
        if any(item.slice_index >= self.slice_count for item in self.cluster_slices):
            raise ValueError("cluster axial slice must be inside the image")
        if len({item.cluster_id for item in self.cluster_slices}) != len(self.cluster_slices):
            raise ValueError("cluster slice bindings must have unique IDs")
        return self


class StatisticalAtlasView(StrictModel):
    atlas_id: str
    name: str
    coordinate_space: str
    available: bool
    voxel_size_mm: tuple[float, float, float] | None
    label_count: int | None
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    reason: str | None = None


class StatisticalAtlasRegionCoverage(StrictModel):
    label_value: int = Field(ge=0)
    label: str = Field(min_length=1)
    voxel_count: int = Field(gt=0)
    proportion: float = Field(gt=0, le=1)


class StatisticalAtlasPeakLocalization(StrictModel):
    cluster_id: str
    peak_coordinate_mm: tuple[float, float, float]
    label_value: int | None
    label: str | None
    atlas_voxel_ijk: tuple[int, int, int] | None
    atlas_voxel_center_mm: tuple[float, float, float] | None
    distance_to_voxel_center_mm: float | None = Field(default=None, ge=0)
    coverage: tuple[StatisticalAtlasRegionCoverage, ...] = ()


class StatisticalResultLocalizationView(StrictModel):
    result_id: str
    result_bundle_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["localized", "no_significant_voxels", "no_reportable_clusters"] = "localized"
    significant_voxel_count: int | None = Field(default=None, ge=0)
    atlas_id: str | None = None
    atlas_name: str | None = None
    atlas_content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    coordinate_space: str | None = None
    localization_method: str = "nearest-neighbor sampling through inverse NIfTI affine"
    clusters: tuple[StatisticalAtlasPeakLocalization, ...]
