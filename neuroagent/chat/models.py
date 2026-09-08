"""Stable internal contracts for Chat Mode."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from neuroagent.context.interfaces import ContextMessage, PinnedContext


class ChatModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChatIntent(StrEnum):
    KNOWLEDGE_QUERY = "knowledge_query"
    CONVERSATION = "conversation"
    WORK_REQUEST = "work_request"


class WorkRequest(ChatModel):
    intent: str = Field(default="work_request", pattern=r"^work_request$")
    task: str = Field(min_length=1, max_length=100)
    parameters: dict[str, Any] = Field(default_factory=dict)
    references: tuple[str, ...] = ()
    status: str = Field(default="draft", pattern=r"^draft$")
    requires_user_confirmation: bool = True


class Citation(ChatModel):
    citation_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    title: str = Field(min_length=1)
    excerpt: str = Field(min_length=1)
    paper_id: str | None = None
    section: str | None = None
    subsection: str | None = None
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)


class ChatAgentRequest(ChatModel):
    session_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=32_000)
    stream: bool = False
    recent_messages: tuple[ContextMessage, ...] = ()
    pinned_context: tuple[PinnedContext, ...] = ()
    preferred_profile_id: str | None = None
    model: str | None = None
    allow_remote_search: bool = False

    @model_validator(mode="after")
    def reject_blank_message(self) -> ChatAgentRequest:
        if not self.message.strip():
            raise ValueError("message must not be blank")
        return self


class ChatAgentResponse(ChatModel):
    session_id: str
    intent: ChatIntent
    answer: str = Field(min_length=1)
    citations: tuple[Citation, ...] = ()
    work_request: WorkRequest | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
