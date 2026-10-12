"""Conversations contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, Field, field_validator, model_validator

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
from neuroagent.domain.conversation import (
    MemoryStatus as MemoryStatus,
)
from neuroagent.domain.execution import QcExecutionMode as QcExecutionMode
from neuroagent.multi_agent.registry import WorkAgentRole

from .plans import SkillPlanIntent
from .projects import WorkspaceSetupIntent
from .shared import StrictModel


class ConversationRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ConversationToolStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    AWAITING_CONFIRMATION = "awaiting_confirmation"


class ConversationCreate(StrictModel):
    mode: ConversationMode
    project_id: str | None = None
    title: str | None = Field(default=None, max_length=200)
    workspace_path: str | None = Field(default=None, max_length=4_000)
    preferred_profile_id: str | None = Field(default=None, max_length=63)


class WorkspaceFileScope(StrictModel):
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


class WorkspaceCitation(StrictModel):
    """A location in a workspace file that can be independently re-read."""

    path: str = Field(min_length=1, max_length=4_000)
    locator: str = Field(min_length=1, max_length=200)
    quote: str | None = Field(default=None, max_length=500)


class WorkspaceContentConsent(StrictModel):
    consent_id: str
    conversation_id: str
    workspace_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_id: str = Field(min_length=1, max_length=63)
    profile_revision: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    model: str = Field(min_length=1, max_length=200)
    policy_version: str
    provider_host: str
    granted_at: datetime
    revoked_at: datetime | None = None
    valid_for_current_context: bool = False


class WorkspaceContentConsentCreate(StrictModel):
    profile_id: str = Field(min_length=1, max_length=63)
    model: str = Field(min_length=1, max_length=200)
    acknowledge_remote_processing: bool = False


class WorkspaceEdit(StrictModel):
    path: str = Field(min_length=1, max_length=4_000)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    replacement: str = Field(max_length=1_048_576)
    after_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_edit(self) -> WorkspaceEdit:
        if self.expected_sha256 is None:
            if self.start_line is not None or self.end_line is not None:
                raise ValueError("new files cannot specify line ranges")
            return self
        if self.start_line is None or self.end_line is None or self.end_line < self.start_line:
            raise ValueError("existing file edits require a valid line range")
        return self


class WorkspaceChangeProposal(StrictModel):
    proposal_id: str
    conversation_id: str
    workspace_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    edits: tuple[WorkspaceEdit, ...] = Field(max_length=200)
    diff: str = Field(max_length=1_048_576)
    citations: tuple[WorkspaceCitation, ...] = ()
    status: str
    expires_at: datetime
    applied_at: datetime | None = None
    undone_at: datetime | None = None


class WorkspacePatchApplyRequest(StrictModel):
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class WorkspacePatchApplyView(StrictModel):
    proposal_id: str
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: str
    applied_at: datetime | None = None
    undo_expires_at: datetime | None = None


class WorkspacePatchUndoView(StrictModel):
    proposal_id: str
    status: str
    undone_at: datetime | None = None


class ConversationMessageView(StrictModel):
    message_id: str
    conversation_id: str
    sequence: int
    role: ConversationRole
    content: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ConversationToolCallView(StrictModel):
    tool_call_id: str
    conversation_id: str
    user_message_id: str
    tool_name: str
    status: ConversationToolStatus
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    # Agent tool-loop telemetry.  ``None`` for single-shot tool calls and for
    # rows persisted before the loop existed.
    step_index: int | None = Field(default=None, ge=1)
    risk_tier: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    agent_role: WorkAgentRole | None = None

    created_at: datetime
    updated_at: datetime


class ConfirmationChallengeView(StrictModel):
    """One-time server-issued authorization for a real Work execution."""

    confirmation_id: str
    conversation_id: str
    project_id: str
    plan_revision_id: str
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    qc_mode: QcExecutionMode
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    consumed_run_id: str | None = None


class ConversationWorkTaskView(StrictModel):
    agent_role: WorkAgentRole
    work_task_id: str | None = None
    draft_hash: str | None = None
    proposal_hash: str | None = None
    proposal_message_id: str | None = None
    plan_revision_id: str | None = None
    run_id: str | None = None


class WorkAgentSelection(StrictModel):
    agent_role: WorkAgentRole
    expected_version: int = Field(ge=1)


class ConversationView(StrictModel):
    conversation_id: str
    mode: ConversationMode
    title: str
    workspace_path: str | None = None
    preferred_profile_id: str | None = None
    project_id: str | None = None
    active_run_id: str | None = None
    foreground_agent_role: WorkAgentRole = WorkAgentRole.PREPROCESSING
    work_tasks: tuple[ConversationWorkTaskView, ...] = ()
    version: int
    created_at: datetime
    updated_at: datetime
    messages: list[ConversationMessageView] = Field(default_factory=list)
    tool_calls: list[ConversationToolCallView] = Field(default_factory=list)


class WorkDraftUpdate(StrictModel):
    base_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    patch: dict[str, Any] = Field(max_length=32)

    @field_validator("patch")
    @classmethod
    def bounded_patch(cls, value: dict[str, Any]) -> dict[str, Any]:
        import json

        if len(json.dumps(value, ensure_ascii=False, allow_nan=False)) > 64_000:
            raise ValueError("draft patch exceeds size limit")
        return value


class WorkConfirmationCreate(StrictModel):
    """UI selection of a server-issued action, never model-generated authority."""

    command: str = Field(min_length=1, max_length=500)
    expected_draft_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class WorkReviewCreate(StrictModel):
    base_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ConversationTurnCreate(StrictModel):
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
    # Set only by an explicit user/UI confirmation; model output is ignored.
    selected_input_stage: str | None = Field(default=None, min_length=1, max_length=64)
    agent_role: WorkAgentRole | None = None

    @model_validator(mode="after")
    def explicit_external_source_requires_analysis(self) -> ConversationTurnCreate:
        if self.external_analysis_import_id is not None and (
            self.target_run_id is not None or self.agent_role is WorkAgentRole.PREPROCESSING
        ):
            raise ValueError("external analysis input cannot select a run or preprocessing role")
        return self


class ConversationTurnView(StrictModel):
    conversation: ConversationView
    user_message: ConversationMessageView
    assistant_message: ConversationMessageView
    tool_call: ConversationToolCallView | None = None
    #: Ordered steps of a bounded Agent tool loop.  Empty for single-shot turns,
    #: where ``tool_call`` carries the one call.
    tool_calls: list[ConversationToolCallView] = Field(default_factory=list)


class MemoryCreate(StrictModel):
    importance: float = Field(default=0.5, ge=0, le=1)
    expires_at: AwareDatetime | None = None
    kind: MemoryKind
    key: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=4_000)
    scope: MemoryScope = MemoryScope.CONVERSATION
    pinned: bool = True
    source_message_id: str | None = None


class MemoryUpdate(StrictModel):
    importance: float | None = Field(default=None, ge=0, le=1)
    expires_at: AwareDatetime | None = None
    action: MemoryAction
    expected_version: int = Field(ge=1)
    content: str | None = Field(default=None, min_length=1, max_length=4_000)


class MemoryView(StrictModel):
    semantic_indexed: bool = False
    importance: float = Field(default=0.5, ge=0, le=1)
    expires_at: datetime | None = None
    proposed_content: str | None = None
    proposal_source_message_id: str | None = None
    memory_id: str
    conversation_id: str
    project_id: str | None = None
    scope: MemoryScope
    kind: MemoryKind
    key: str
    content: str
    status: MemoryStatus
    pinned: bool
    source_message_id: str | None = None
    confidence: float = Field(ge=0, le=1)
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class ContextSummaryView(StrictModel):
    summary_id: str
    conversation_id: str
    content: str
    covered_sequence: int = Field(ge=0)
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    method: str
    created_at: datetime


class ConversationContextView(StrictModel):
    summary: ContextSummaryView | None = None
    memories: list[MemoryView] = Field(default_factory=list)
