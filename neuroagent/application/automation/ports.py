"""Persistence ports for durable Work task progress."""

from __future__ import annotations

from typing import Protocol

from neuroagent.application.automation.models import (
    EventConsumptionDecision,
    WorkEventIdentity,
    WorkTask,
)


class WorkTaskRepositoryPort(Protocol):
    """Store goal progress independently from the authoritative workflow state."""

    def create_task(self, task: WorkTask) -> WorkTask: ...

    def get_task(self, task_id: str) -> WorkTask: ...

    def save_task(self, task: WorkTask, *, expected_version: int) -> WorkTask: ...

    def consume_event(
        self,
        task_id: str,
        event: WorkEventIdentity,
        *,
        expected_version: int,
        updated_task: WorkTask,
    ) -> tuple[EventConsumptionDecision, WorkTask | None]: ...
