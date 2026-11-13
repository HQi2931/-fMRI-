"""Transport-owned request DTOs for analysis helper endpoints."""

from __future__ import annotations

from pydantic import Field

from neuroagent.analysis.models import (
    AtlasPoint,
    ClusterRecord,
    MlDesignRecommendation,
    RoiExtractionRequest,
    RoiSignalRecord,
)
from neuroagent.api.schemas.shared import StrictHttpModel


class RunDiagnosisRequest(StrictHttpModel):
    """A bounded log excerpt supplied by the UI for deterministic diagnosis."""

    log_text: str = Field(min_length=1, max_length=200_000)


class MlTableInspectRequest(StrictHttpModel):
    project_id: str
    source_path: str
    max_rows: int = Field(default=100_000, ge=1, le=100_000)


class MlTemplateCreateRequest(StrictHttpModel):
    design: MlDesignRecommendation
    source_filename: str = Field(
        default="features.csv",
        pattern=r"^[A-Za-z0-9_.-]+\.(csv|tsv|xlsx)$",
    )


class RoiTableCreateRequest(StrictHttpModel):
    design: RoiExtractionRequest
    records: tuple[RoiSignalRecord, ...] = Field(min_length=1, max_length=1_000_000)


class ClusterLocalizationRequest(StrictHttpModel):
    clusters: tuple[ClusterRecord, ...] = Field(min_length=1, max_length=100_000)
    atlas_points: tuple[AtlasPoint, ...] = ()
    max_distance_mm: float = Field(default=8, gt=0, le=100)
