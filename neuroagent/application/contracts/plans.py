"""Plans contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from neuroagent.domain.fmri.metrics import AlffFalffParameters, MetricKind, RehoParameters
from neuroagent.domain.fmri.preprocessing import PreprocessingParameters
from neuroagent.domain.lifecycle import ApprovalDecision as ApprovalDecision
from neuroagent.domain.lifecycle import PlanState
from neuroagent.skills.models import SkillPlan

from .shared import StrictModel


class PlanRevisionCreate(StrictModel):
    project_id: str
    expected_project_version: int = Field(ge=1)
    plan: dict[str, Any]
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    supersedes_plan_revision_id: str | None = None

    @field_validator("plan")
    @classmethod
    def reject_executable_text(cls, value: dict[str, Any]) -> dict[str, Any]:
        forbidden = {"shell", "command", "matlab_script", "script_text", "code"}
        present: set[str] = set()

        def inspect(item: Any) -> None:
            if isinstance(item, dict):
                present.update(forbidden.intersection(str(key).lower() for key in item))
                for nested in item.values():
                    inspect(nested)
            elif isinstance(item, list):
                for nested in item:
                    inspect(nested)

        inspect(value)
        if present:
            raise ValueError(f"free executable text is forbidden: {sorted(present)}")
        return value


class ValidationIssue(StrictModel):
    code: str
    message: str
    severity: str = Field(pattern="^(info|warning|blocking)$")
    path: str | None = None


class PlanValidationRequest(StrictModel):
    expected_version: int = Field(ge=1)
    issues: list[ValidationIssue] = Field(default_factory=list)


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


class ApprovalCreate(StrictModel):
    expected_version: int = Field(ge=1)
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor: str = Field(min_length=1, max_length=200)
    decision: ApprovalDecision
    reason: str = Field(min_length=1, max_length=2000)


class ApprovalView(StrictModel):
    approval_id: str
    plan_revision_id: str
    plan_hash: str
    actor: str
    decision: ApprovalDecision
    reason: str
    created_at: datetime


class SkillPlanIntent(StrictModel):
    """Client intent without client-authored Artifact lineage or environment locks."""

    project_id: str = Field(min_length=1)
    dataset_ref: str = Field(min_length=1)
    input_manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    requested_metrics: tuple[MetricKind, ...]
    primary_outputs: tuple[str, ...]
    input_artifact_id: str | None = None
    alff_falff: AlffFalffParameters | None = None
    reho: RehoParameters | None = None
    request_preprocessing: bool = False
    preprocessing: PreprocessingParameters | None = None

    @model_validator(mode="after")
    def intent_is_complete(self) -> SkillPlanIntent:
        if self.request_preprocessing:
            if self.preprocessing is None:
                raise ValueError("preprocessing parameters are required")
            if self.input_artifact_id is not None:
                raise ValueError(
                    "initial preprocessing input is derived from the frozen dataset manifest"
                )
        else:
            if self.preprocessing is not None:
                raise ValueError("preprocessing parameters require request_preprocessing=true")
            if self.input_artifact_id is None:
                raise ValueError("metric-only planning requires a registered input Artifact ID")
        return self


class SkillPlanResolveRequest(StrictModel):
    request: SkillPlanIntent
    expected_project_version: int = Field(ge=1)
    supersedes_plan_revision_id: str | None = None
    validation_mode: Literal["strict", "report_only"] = "strict"


class SkillPlanResolveView(StrictModel):
    skill_plan: SkillPlan
    plan_revision: PlanRevisionView


class SkillPlanDryRunView(StrictModel):
    """Read-only preprocessing Skill contract validation and compilation result."""

    scope: Literal["skill_contract_compile_only"] = "skill_contract_compile_only"
    environment_probed: Literal[False] = False
    manifest_id: str
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    ready: bool
    plan_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    issues: tuple[ValidationIssue, ...] = ()
