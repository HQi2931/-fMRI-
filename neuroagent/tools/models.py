"""Typed definitions for deterministic registered tools."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from neuroagent.skills.models import ResolvedToolRef, stable_hash


class ToolRisk(StrEnum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    COMPUTE = "compute"


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    capability: str = Field(min_length=1)
    risk: ToolRisk
    supports_dry_run: bool
    timeout_seconds: int = Field(gt=0)
    input_schema_ref: str = Field(min_length=1)
    output_schema_ref: str = Field(min_length=1)

    def as_resolved_ref(self) -> ResolvedToolRef:
        return ResolvedToolRef(
            capability=self.capability,
            tool_id=self.tool_id,
            version=self.version,
            content_hash=stable_hash(self.model_dump(mode="json")),
        )
