"""Persistence ports for durable Work task progress."""

from __future__ import annotations

from typing import Protocol

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
