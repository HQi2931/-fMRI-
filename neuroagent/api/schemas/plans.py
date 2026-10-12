"""HTTP request schemas for plan approval and SkillPlan commands."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from neuroagent.api.schemas.shared import StrictHttpModel
from neuroagent.domain.execution import ExecutionBackend
from neuroagent.domain.fmri.metrics import AlffFalffParameters, MetricKind, RehoParameters
from neuroagent.domain.fmri.preprocessing import PreprocessingParameters
from neuroagent.domain.fmri.statistics import CorrectionSpec, StatisticalDesignRevision
from neuroagent.domain.lifecycle import ApprovalDecision


class ApprovalCreate(StrictHttpModel):
    expected_version: int = Field(ge=1)
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor: str = Field(min_length=1, max_length=200)
    decision: ApprovalDecision
    reason: str = Field(min_length=1, max_length=2000)


class SkillPlanIntent(StrictHttpModel):
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


class SkillPlanResolveRequest(StrictHttpModel):
    request: SkillPlanIntent
    expected_project_version: int = Field(ge=1)
    supersedes_plan_revision_id: str | None = None
    validation_mode: Literal["strict", "report_only"] = "strict"


class StatisticalWorkContext(StrictHttpModel):
    conversation_id: str = Field(min_length=1)
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class StatisticalDesignCreate(StrictHttpModel):
    project_id: str
    expected_project_version: int = Field(ge=1)
    input_manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    design: StatisticalDesignRevision
    correction: CorrectionSpec | None = None
    atlas_id: Literal["aal", "aal3", "brodmann"] | None = None
    supersedes_plan_revision_id: str | None = None
    work_context: StatisticalWorkContext | None = None


class StatisticalDesignValidationRequest(StrictHttpModel):
    expected_version: int = Field(ge=1)


class StatisticsRunCreate(StrictHttpModel):
    project_id: str
    statistical_design_revision_id: str
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    max_attempts: int = Field(default=1, ge=1, le=5)
    execution_backend: ExecutionBackend = ExecutionBackend.MOCK
    real_execution_confirmed: bool = False

    @model_validator(mode="after")
    def require_real_execution_confirmation(self) -> StatisticsRunCreate:
        if self.execution_backend is ExecutionBackend.MATLAB and not self.real_execution_confirmed:
            raise ValueError("real MATLAB execution requires explicit confirmation")
        return self
