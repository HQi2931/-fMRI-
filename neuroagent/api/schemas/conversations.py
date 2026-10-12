"""HTTP request schemas for conversations, Work actions, and memory."""

from __future__ import annotations

import json
from typing import Any

from pydantic import AwareDatetime, Field, field_validator, model_validator

from neuroagent.api.schemas.plans import SkillPlanIntent
from neuroagent.api.schemas.projects import WorkspaceSetupIntent
from neuroagent.api.schemas.shared import StrictHttpModel
from neuroagent.domain.conversation import (
    ConversationAction as ConversationAction,
)
from neuroagent.domain.conversation import (
    ConversationMode as ConversationMode,
)
from neuroagent.domain.conversation import (
    MemoryAction as MemoryAction,
)
from neuroagent.domain.conversation import (
    MemoryKind as MemoryKind,
)
from neuroagent.domain.conversation import (
    MemoryScope as MemoryScope,
)
from neuroagent.domain.execution import QcExecutionMode
from neuroagent.multi_agent.registry import WorkAgentRole


class ConversationCreate(StrictHttpModel):
    mode: ConversationMode
    project_id: str | None = None
    title: str | None = Field(default=None, max_length=200)
    workspace_path: str | None = Field(default=None, max_length=4_000)
    preferred_profile_id: str | None = Field(default=None, max_length=63)


class WorkspaceFileScope(StrictHttpModel):
    """Optional user-selected file scope for the Work file assistant."""

    paths: tuple[str, ...] = Field(default=(), max_length=200)

    @field_validator("paths")
    @classmethod
    def relative_paths_only(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for path in value:
            if (
                not path
                or path.startswith(("/", "\\"))
                or ":" in path
                or "\\" in path
                or any(part in {"", ".", ".."} for part in path.split("/"))
            ):
                raise ValueError("workspace scope paths must be relative POSIX paths")
        return value


class WorkspaceContentConsentCreate(StrictHttpModel):
    profile_id: str = Field(min_length=1, max_length=63)
    model: str = Field(min_length=1, max_length=200)
    acknowledge_remote_processing: bool = False


class WorkspacePatchApplyRequest(StrictHttpModel):
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class WorkAgentSelection(StrictHttpModel):
    agent_role: WorkAgentRole
    expected_version: int = Field(ge=1)


class WorkDraftUpdate(StrictHttpModel):
    base_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    patch: dict[str, Any] = Field(max_length=32)

    @field_validator("patch")
    @classmethod
    def bounded_patch(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False)) > 64_000:
            raise ValueError("draft patch exceeds size limit")
        return value


class WorkConfirmationCreate(StrictHttpModel):
    """UI selection of a server-issued action, never model-generated authority."""

    command: str = Field(min_length=1, max_length=500)
    expected_draft_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class WorkReviewCreate(StrictHttpModel):
    base_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ConversationTurnCreate(StrictHttpModel):
    paper_ids: tuple[str, ...] = ()
    content: str = Field(min_length=1, max_length=8_000, pattern=r"\S")
    stream: bool = False
    action: ConversationAction = ConversationAction.AUTO
    allow_remote_search: bool = False
    model: str | None = Field(default=None, min_length=1, max_length=200)
    workspace_path: str | None = Field(default=None, max_length=4_000)
    preferred_profile_id: str | None = Field(default=None, max_length=63)
    project_id: str | None = None
    target_run_id: str | None = None
    external_analysis_import_id: str | None = Field(default=None, min_length=1, max_length=64)
    qc_review_id: str | None = None
    workspace_setup: WorkspaceSetupIntent | None = None
    skill_plan_intent: SkillPlanIntent | None = None
    expected_project_version: int | None = Field(default=None, ge=1)
    plan_revision_id: str | None = None
    expected_plan_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    real_execution_confirmed: bool = False
    qc_mode: QcExecutionMode | None = None
    confirmation_id: str | None = Field(default=None, min_length=1, max_length=64)
    workspace_file_scope: WorkspaceFileScope | None = None
    selected_input_stage: str | None = Field(default=None, min_length=1, max_length=64)
    agent_role: WorkAgentRole | None = None

    @model_validator(mode="after")
    def explicit_external_source_requires_analysis(self) -> ConversationTurnCreate:
        if self.external_analysis_import_id is not None and (
            self.target_run_id is not None or self.agent_role is WorkAgentRole.PREPROCESSING
        ):
            raise ValueError("external analysis input cannot select a run or preprocessing role")
        return self


class MemoryCreate(StrictHttpModel):
    importance: float = Field(default=0.5, ge=0, le=1)
    expires_at: AwareDatetime | None = None
    kind: MemoryKind
    key: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=4_000)
    scope: MemoryScope = MemoryScope.CONVERSATION
    pinned: bool = True
    source_message_id: str | None = None


class MemoryUpdate(StrictHttpModel):
    importance: float | None = Field(default=None, ge=0, le=1)
    expires_at: AwareDatetime | None = None
    action: MemoryAction
    expected_version: int = Field(ge=1)
    content: str | None = Field(default=None, min_length=1, max_length=4_000)
