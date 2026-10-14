"""Persistence ports for durable Work task progress."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

from neuroagent.application.automation.analysis_outbox import (
    AnalysisOutboxClaim,
    AnalysisProposalOutbox,
)
from neuroagent.application.automation.models import (
    EventConsumptionDecision,
    WorkEventIdentity,
    WorkTask,
    WorkTaskClaim,
)


class WorkTaskRepositoryPort(Protocol):
    """Store goal progress independently from the authoritative workflow state."""

    def create_task(self, task: WorkTask) -> WorkTask: ...

    def get_task(self, task_id: str) -> WorkTask: ...

    def save_task(
        self,
        task: WorkTask,
        *,
        expected_version: int,
        claim_token: str | None = None,
    ) -> WorkTask: ...

    def claim_next_reconciliation_task(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
        cooldown_seconds: float,
    ) -> WorkTaskClaim | None: ...

    def release_reconciliation_claim(
        self, task_id: str, worker_id: str, claim_token: str
    ) -> bool: ...

    def consume_event(
        self,
        task_id: str,
        event: WorkEventIdentity,
        *,
        expected_version: int,
        updated_task: WorkTask,
        claim_token: str | None = None,
    ) -> tuple[EventConsumptionDecision, WorkTask | None]: ...

    def ensure_analysis_outbox(self, record: AnalysisProposalOutbox) -> AnalysisProposalOutbox: ...

    def get_analysis_outbox(self, intent_task_id: str) -> AnalysisProposalOutbox: ...

    def get_analysis_outbox_for_preprocessing_task(
        self, preprocessing_task_id: str
    ) -> AnalysisProposalOutbox | None: ...

    def mark_analysis_outbox_ready(
        self,
        intent_task_id: str,
        *,
        preprocessing_task_id: str,
        plan_revision_id: str,
        manifest_hash: str,
        run_id: str,
        artifact_ids: tuple[str, ...],
    ) -> AnalysisProposalOutbox: ...

    def claim_analysis_outbox(
        self, worker_id: str, *, lease_seconds: int
    ) -> AnalysisOutboxClaim | None: ...

    def validate_analysis_outbox_claim(self, claim: AnalysisOutboxClaim) -> bool: ...

    def atomic(self) -> AbstractContextManager[None]: ...
