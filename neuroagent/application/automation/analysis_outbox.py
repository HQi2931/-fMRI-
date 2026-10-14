"""Proposal-only continuation records for explicit two-stage Work intents."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class AnalysisOutboxStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"


class AnalysisProposalOutbox(BaseModel):
    """References an exact conversation intent and verified preprocessing evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_task_id: str = Field(min_length=1, max_length=100)
    conversation_id: str = Field(min_length=1, max_length=100)
    project_id: str = Field(min_length=1, max_length=100)
    preprocessing_task_id: str = Field(min_length=1, max_length=100)
    plan_revision_id: str = Field(min_length=1, max_length=100)
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    run_id: str = Field(min_length=1, max_length=100)
    analysis_request_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_ids: tuple[str, ...] = Field(default=(), max_length=20_000)
    status: AnalysisOutboxStatus = AnalysisOutboxStatus.PENDING
    lease_owner: str | None = Field(default=None, max_length=200)
    lease_expires_at: float | None = None
    fencing_token: int = Field(default=0, ge=0)


class AnalysisOutboxClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_task_id: str
    worker_id: str
    lease_expires_at: float
    fencing_token: int
