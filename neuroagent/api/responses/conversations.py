"""Conversations contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from neuroagent.api.schemas.shared import StrictHttpModel as StrictModel
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


class ConversationRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class CompositeGoalProgressStatus(StrEnum):
    WAITING_PREPROCESSING = "waiting_preprocessing"
    WAITING_ANALYSIS = "waiting_analysis"
    WAITING_RESULT_INTERPRETATION = "waiting_result_interpretation"
    WAITING_USER = "waiting_user"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    INDETERMINATE = "indeterminate"


class CompositeGoalProgressView(StrictModel):
    """Conversation-scoped stable identity and current composite-goal projection."""

    analysis_summary: str | None = Field(
        default=None,
        max_length=160,
        description=(
            "Bounded summary from the exact persisted analysis intent message; null when its "
            "conversation, project, intent ID, or request hash cannot be verified."
        ),
    )
    intent_task_id: str
    conversation_id: str
    project_id: str
    analysis_request_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    goal_kind: Literal["preprocess_then_analyze", "analysis_only_external_import"] = (
        "preprocess_then_analyze"
    )
    preprocessing_task_id: str | None = None
    source_external_import_id: str | None = None
    source_import_version: int | None = None
    source_tree_hash: str | None = None
    source_inclusion_hash: str | None = None
    source_manifest_hash: str | None = None
    source_revision_hash: str | None = None
    analysis_proposal_outbox_intent_task_id: str | None = None
    analysis_work_task_id: str | None = None
    result_interpretation_outbox_task_id: str | None = None
    status: CompositeGoalProgressStatus
    preprocessing_status: str | None = None
    analysis_proposal_status: str | None = None
    analysis_status: str | None = None
    result_interpretation_status: str | None = None
    continuation_message_id: str | None = None
    reason_code: str | None = None


class WorkspaceFileEntryResponse(StrictModel):
    path: str
    name: str
    kind: Literal["file", "directory"]
    size: int | None = None
    format: str | None = None
    readable: bool | None = None


class WorkspaceFileListingResponse(StrictModel):
    directory: str
    entries: list[WorkspaceFileEntryResponse]
    total: int
    next_offset: int | None = None


class ConversationToolStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    AWAITING_CONFIRMATION = "awaiting_confirmation"


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


class ConversationWorkTaskView(StrictModel):
    agent_role: WorkAgentRole
    analysis_intent_task_id: str | None = Field(default=None, min_length=1, max_length=100)
    work_task_id: str | None = None
    draft_hash: str | None = None
    proposal_hash: str | None = None
    proposal_message_id: str | None = None
    plan_revision_id: str | None = None
    run_id: str | None = None


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


class ConversationTurnView(StrictModel):
    conversation: ConversationView
    user_message: ConversationMessageView
    assistant_message: ConversationMessageView
    tool_call: ConversationToolCallView | None = None
    #: Ordered steps of a bounded Agent tool loop.  Empty for single-shot turns,
    #: where ``tool_call`` carries the one call.
    tool_calls: list[ConversationToolCallView] = Field(default_factory=list)


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
