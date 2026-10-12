"""Transport schemas for inspecting and including external data."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from neuroagent.api.schemas.shared import StrictHttpModel
from neuroagent.domain.fmri.external_import import ExternalCoordinateSpace


class ExternalAnalysisImportInspectRequest(StrictHttpModel):
    """Inspect a user-selected external DPABI metric directory without inclusion."""

    project_id: str = Field(min_length=1)
    source_path: str = Field(min_length=1, max_length=4096)
    layout: Literal["grouped", "paired_conditions"] = "grouped"


class ExternalAnalysisImportIncludeRequest(StrictHttpModel):
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
