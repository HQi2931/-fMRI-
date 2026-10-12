"""Strict model-output contracts for the Work roles."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from neuroagent.application.contracts.conversations import WorkspaceCitation
from neuroagent.application.work_actions import is_model_selectable


class WorkQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=400)
    parameter_paths: tuple[str, ...] = Field(default=(), max_length=8)


class WorkInterpretation(BaseModel):
    """A role response that can propose changes but cannot authorize execution."""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=12000)
    action: str = Field(default="conversation", max_length=64)
    draft_patch: dict[str, Any] = Field(default_factory=dict)
    analysis_patch: dict[str, Any] = Field(default_factory=dict)
    handoff_reason: str | None = Field(default=None, max_length=1000)
    required_preprocessing_outputs: tuple[str, ...] = Field(default=(), max_length=12)
    workspace_citations: tuple[WorkspaceCitation, ...] = ()
    workspace_edits: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    recommended_input_stage: str | None = Field(default=None, max_length=64)
    stage_intent: str | None = Field(default=None, max_length=1000)
    stage_reasoning: str | None = Field(default=None, max_length=4000)
    stage_questions: tuple[str, ...] = ()
    questions: tuple[WorkQuestion | str, ...] = Field(default=(), max_length=5)
    recommendation_reason: str | None = Field(default=None, max_length=1000)

    @field_validator("action")
    @classmethod
    def action_must_be_model_selectable(cls, value: str) -> str:
        if not is_model_selectable(value):
            raise ValueError(f"action {value!r} is not available to a model")
        return value
