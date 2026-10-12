"""Runs contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from neuroagent.api.schemas.shared import StrictHttpModel as StrictModel
from neuroagent.domain.execution import (
    ExecutionBackend as ExecutionBackend,
)
from neuroagent.domain.execution import (
    ExecutionWorkspaceMode as ExecutionWorkspaceMode,
)
from neuroagent.domain.execution import (
    MockOutcome as MockOutcome,
)
from neuroagent.domain.execution import (
    QcExecutionMode as QcExecutionMode,
)
from neuroagent.domain.fmri.qc import QcReviewRevision
from neuroagent.domain.lifecycle import WorkflowState


class QcReviewState(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


class QcSkipView(StrictModel):
    qc_skip_id: str
    run_id: str
    project_id: str
    run_version: int = Field(ge=1)
    decision_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor: str
    reason: str
    qc_review_revision_id: str | None = None
    created_at: datetime


class QcReviewView(StrictModel):
    review: QcReviewRevision
    run_id: str
    project_id: str
    revision: int
    version: int
    state: QcReviewState
    created_at: datetime
    updated_at: datetime


class RunView(StrictModel):
    run_id: str
    project_id: str
    plan_revision_id: str
    state: WorkflowState
    version: int
    attempt: int
    cancel_requested: bool
    error: str | None = None
    stage: str = "queued"
    stage_progress: float | None = Field(default=None, ge=0, le=1)
    heartbeat: datetime | None = None
    log_cursor: int = Field(default=0, ge=0)
    eta_seconds: int | None = Field(default=None, ge=0)
    diagnosis_available: bool = False
    qc_mode: QcExecutionMode = QcExecutionMode.REQUIRE
    qc_status: str = "pending"
    created_at: datetime
    updated_at: datetime


class ArtifactView(StrictModel):
    artifact_id: str
    project_id: str
    run_id: str
    artifact_type: str
    relative_path: str
    checksum: str
    size_bytes: int
    provenance: dict[str, Any]
    created_at: datetime


class RuntimeEventView(StrictModel):
    event_id: int
    trace_id: str
    project_id: str | None = None
    run_id: str | None = None
    event_type: str
    severity: str
    payload: dict[str, Any]
    created_at: datetime
