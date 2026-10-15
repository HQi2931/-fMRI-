"""Proposal-only continuation records for explicit two-stage Work intents."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class AnalysisOutboxStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    PROPOSAL_READY = "proposal_ready"
    WAITING_USER = "waiting_user"
    BLOCKED = "blocked"


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
    attempt_count: int = Field(default=0, ge=0)
    retry_generation: int = Field(default=0, ge=0)
    next_attempt_at: float | None = None
    last_error_code: str | None = Field(default=None, max_length=64)
    continuation_message_id: str | None = Field(default=None, max_length=100)
    analysis_work_task_id: str | None = Field(default=None, max_length=100)


class AnalysisOutboxRetryRequest(BaseModel):
    """User-triggered retry identity, bounded to one exact outbox."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_task_id: str = Field(min_length=1, max_length=100)


class AnalysisOutboxClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_task_id: str
    worker_id: str
    lease_expires_at: float
    fencing_token: int


class AnalysisOutboxRegistrationSource(BaseModel):
    """Versioned preprocessing authority captured before an intent is committed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    preprocessing_task_id: str = Field(min_length=1, max_length=100)
    task_version: int = Field(ge=1)
    preprocessing_step_id: str = Field(min_length=1, max_length=100)
    step_version: int = Field(ge=1)
    task_status: str = Field(min_length=1, max_length=32)
    step_status: str = Field(min_length=1, max_length=32)
    run_id: str = Field(min_length=1, max_length=100)
    run_version: int = Field(ge=1)
    run_state: str = Field(min_length=1, max_length=32)
    plan_revision_id: str = Field(min_length=1, max_length=100)
    plan_version: int = Field(ge=1)
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    ready_artifact_ids: tuple[str, ...] = Field(default=(), max_length=20_000)
    completion_evidence_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class AnalysisSourceModelFacts(BaseModel):
    """Small, identifier-free source summary that is safe to put in model context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_run_state: str = Field(max_length=32)
    qc_status: str = Field(max_length=32)
    qc_review_state: str | None = Field(default=None, max_length=32)
    input_manifest_verified: bool
    metric_count: int = Field(ge=0, le=20_000)
    metric_types: tuple[str, ...] = Field(max_length=64)
    subject_count: int = Field(ge=0, le=20_000)
    mask_count: int = Field(ge=0, le=20_000)
    verified_artifact_count: int = Field(ge=0, le=40_000)

    @classmethod
    def summarize(
        cls,
        *,
        source_run_state: str,
        qc_status: str,
        qc_review_state: str | None,
        input_manifest_verified: bool,
        metric_count: int,
        metric_artifact_types: Iterable[str],
        subject_count: int,
        mask_count: int,
        verified_artifact_count: int,
    ) -> AnalysisSourceModelFacts:
        """Keep artifact IDs, paths, hashes and per-subject lineage server-side."""

        return cls(
            source_run_state=source_run_state,
            qc_status=qc_status,
            qc_review_state=qc_review_state,
            input_manifest_verified=input_manifest_verified,
            metric_count=metric_count,
            metric_types=tuple(sorted(set(metric_artifact_types))),
            subject_count=subject_count,
            mask_count=mask_count,
            verified_artifact_count=verified_artifact_count,
        )


class VerifiedAnalysisOutboxSource(BaseModel):
    """Exact request and server-verified source evidence for one continuation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: str
    project_id: str
    intent_task_id: str
    preprocessing_task_id: str
    retry_generation: int = Field(default=0, ge=0)
    analysis_request: str = Field(min_length=1, max_length=8_000)
    run_id: str
    plan_revision_id: str
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_ids: tuple[str, ...] = Field(min_length=1, max_length=20_000)
    dependency_artifact_ids: tuple[str, ...] = Field(default=(), max_length=20_000)
    source_evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_facts: AnalysisSourceModelFacts
