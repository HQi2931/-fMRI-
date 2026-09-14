"""Validated candidates before they become durable memory records."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from neuroagent.application.contracts import MemoryKind, MemoryScope, MemoryStatus


class MemoryCandidateDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: MemoryKind
    key: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_.-]+$")
    content: str = Field(min_length=1, max_length=4_000)
    scope: MemoryScope = MemoryScope.CONVERSATION
    confidence: float = Field(default=0.5, ge=0, le=1)
    importance: float = Field(default=0.5, ge=0, le=1)
    status: MemoryStatus = MemoryStatus.PENDING
    pinned: bool = False


class MemoryCandidateBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidates: tuple[MemoryCandidateDraft, ...] = Field(default=(), max_length=8)
