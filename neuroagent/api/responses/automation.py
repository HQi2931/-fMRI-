"""HTTP response DTOs for durable WorkTask automation."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from neuroagent.api.responses.plans import PlanRevisionView
from neuroagent.api.schemas.shared import StrictHttpModel
from neuroagent.application.automation.models import TaskStatus, TaskStepStatus
from neuroagent.domain.fmri.preprocessing_outputs import DpabiPreprocessingOutput
from neuroagent.domain.lifecycle import PlanState


class StepPlanReviewView(StrictHttpModel):
    """Read-only snapshot for rendering the exact Plan bound to a READY step."""

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
