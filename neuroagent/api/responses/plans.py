"""Plans contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from neuroagent.api.schemas.shared import StrictHttpModel as StrictModel
from neuroagent.domain.lifecycle import ApprovalDecision as ApprovalDecision
from neuroagent.domain.lifecycle import PlanState
from neuroagent.skills.models import SkillPlan


class ValidationIssue(StrictModel):
    code: str
    message: str
    severity: str = Field(pattern="^(info|warning|blocking)$")
    path: str | None = None


class PlanRevisionView(StrictModel):
    plan_revision_id: str
    project_id: str
    revision: int
    version: int
    plan_hash: str
    manifest_hash: str
    environment_hash: str
    state: PlanState
    plan: dict[str, Any]
    validation_issues: list[ValidationIssue]
    supersedes_plan_revision_id: str | None = None
    created_at: datetime
    updated_at: datetime


class ApprovalView(StrictModel):
    approval_id: str
    plan_revision_id: str
    plan_hash: str
    actor: str
    decision: ApprovalDecision
    reason: str
    created_at: datetime


class SkillPlanResolveView(StrictModel):
    skill_plan: SkillPlan
    plan_revision: PlanRevisionView
