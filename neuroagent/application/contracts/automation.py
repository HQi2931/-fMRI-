"""Read-only contracts for persistent WorkTask automation."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from neuroagent.application.automation.models import TaskStatus, TaskStepStatus
from neuroagent.application.contracts.plans import PlanRevisionView
from neuroagent.application.contracts.shared import StrictModel
from neuroagent.domain.fmri.preprocessing_outputs import DpabiPreprocessingOutput
from neuroagent.domain.lifecycle import PlanState


class StepPlanReviewView(StrictModel):
    """Authoritative snapshot of one READY step waiting for its Plan review."""

    conversation_id: str
    project_id: str
    task_id: str
    task_version: int
    task_status: TaskStatus
    step_id: str
    step_sequence: int
    step_kind: str
    step_status: TaskStepStatus
    step_version: int
    step_input_version: str | None
    input_revision: str
    waiting_reference: str
    plan_revision_id: str
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_state: PlanState
    execution_authorized: Literal[False] = False
    expected_artifacts: tuple[DpabiPreprocessingOutput, ...] = ()
    plan: PlanRevisionView
