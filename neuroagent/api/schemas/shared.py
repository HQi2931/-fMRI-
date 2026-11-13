"""Transport-level error envelope models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StrictHttpModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorBody(StrictHttpModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None


class ErrorResponse(StrictHttpModel):
    error: ErrorBody
