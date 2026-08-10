"""Versioned, immutable Skill contracts."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from neuroagent.domain.fmri.artifacts import ArtifactLineage
from neuroagent.domain.fmri.metrics import AlffFalffParameters, MetricKind, RehoParameters
from neuroagent.domain.fmri.preprocessing import PreprocessingParameters


class FrozenSkillModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SkillStatus(StrEnum):
    DRAFT = "draft"
    REVIEWED = "reviewed"
    DEPRECATED = "deprecated"


class IssueSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class SkillValidationIssue(FrozenSkillModel):
    code: str = Field(min_length=1)
    severity: IssueSeverity
    message: str = Field(min_length=1)
    path: str | None = None
    evidence_ref: str | None = None
    remediation: str | None = None


class ValidationReport(FrozenSkillModel):
    issues: tuple[SkillValidationIssue, ...] = ()

    @property
    def has_blockers(self) -> bool:
        return any(issue.severity is IssueSeverity.BLOCKING for issue in self.issues)


class ArtifactContract(FrozenSkillModel):
    name: str = Field(min_length=1)
    artifact_type: str = Field(min_length=1)
    required_lineage: tuple[str, ...] = ()


class SkillStep(FrozenSkillModel):
    step_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    capability: str = Field(min_length=1)
    needs: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    parameter_names: tuple[str, ...] = ()
    qc_gate: bool = False

    @model_validator(mode="after")
    def no_self_dependency(self) -> SkillStep:
        if self.step_id in self.needs:
            raise ValueError("a Skill step cannot depend on itself")
        return self


class SkillCompatibility(FrozenSkillModel):
    matlab: str
    spm: str
    dpabi: str
    adapter: str


class SkillSpec(FrozenSkillModel):
    schema_version: str = Field(pattern=r"^1\.0$")
    skill_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    title: str = Field(min_length=1)
    status: SkillStatus
    requested_metrics: tuple[MetricKind, ...] = ()
    required_parameters: tuple[str, ...]
    input_artifacts: tuple[ArtifactContract, ...]
    output_artifacts: tuple[ArtifactContract, ...]
    required_capabilities: tuple[str, ...]
    workflow_template_ref: str = Field(min_length=1)
    steps: tuple[SkillStep, ...]
    qc_requirements: tuple[str, ...]
    compatibility: SkillCompatibility
    evidence_refs: tuple[str, ...]
    known_limitations: tuple[str, ...]
    reviewed_by: tuple[str, ...]

    @model_validator(mode="after")
    def internally_consistent(self) -> SkillSpec:
        step_ids = {step.step_id for step in self.steps}
        if len(step_ids) != len(self.steps):
            raise ValueError("Skill step IDs must be unique")
        missing_dependencies = {
            dependency
            for step in self.steps
            for dependency in step.needs
            if dependency not in step_ids
        }
        if missing_dependencies:
            raise ValueError(f"Skill has missing step dependencies: {sorted(missing_dependencies)}")
        declared = set(self.required_capabilities)
        used = {step.capability for step in self.steps}
        if used - declared:
            raise ValueError(f"steps use undeclared capabilities: {sorted(used - declared)}")
        _topological_order(self.steps)
        if self.status is SkillStatus.REVIEWED and not self.reviewed_by:
            raise ValueError("a reviewed Skill requires reviewer evidence")
        return self

    @property
    def content_hash(self) -> str:
        return stable_hash(self.model_dump(mode="json"))


class EnvironmentSnapshot(FrozenSkillModel):
    matlab_version: str = Field(min_length=1)
    spm_version: str = Field(min_length=1)
    dpabi_version: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    environment_hash: str = Field(min_length=64, max_length=64)


class SkillRequest(FrozenSkillModel):
    project_id: str = Field(min_length=1)
    dataset_ref: str = Field(min_length=1)
    input_manifest_hash: str = Field(min_length=64, max_length=64)
    requested_metrics: tuple[MetricKind, ...]
    primary_outputs: tuple[str, ...]
    input_artifact: ArtifactLineage
    alff_falff: AlffFalffParameters | None
    reho: RehoParameters | None
    study_protocol_ref: str = Field(min_length=1)
    request_preprocessing: bool = False
    preprocessing: PreprocessingParameters | None = None
    base_cfg_artifact_id: str | None = None

    @model_validator(mode="after")
    def parameters_match_goals(self) -> SkillRequest:
        if len(set(self.requested_metrics)) != len(self.requested_metrics):
            raise ValueError("requested_metrics must be unique")
        if not self.requested_metrics and not self.request_preprocessing:
            raise ValueError("request must select preprocessing and/or at least one metric")
        if ({MetricKind.ALFF, MetricKind.FALFF} & set(self.requested_metrics)) and not (
            self.alff_falff
        ):
            raise ValueError("ALFF/fALFF metrics require alff_falff parameters")
        if MetricKind.REHO in self.requested_metrics and self.reho is None:
            raise ValueError("ReHo metric requires reho parameters")
        if self.request_preprocessing:
            if self.preprocessing is None or not self.base_cfg_artifact_id:
                raise ValueError(
                    "preprocessing requires typed parameters and a base_cfg_artifact_id"
                )
        elif self.preprocessing is not None or self.base_cfg_artifact_id is not None:
            raise ValueError(
                "preprocessing parameters and base Cfg require request_preprocessing=true"
            )
        if self.input_manifest_hash != self.input_artifact.subject_manifest_hash:
            raise ValueError("input artifact lineage does not match the frozen manifest")
        return self


class SkillResolution(FrozenSkillModel):
    request: SkillRequest
    environment: EnvironmentSnapshot
    selected_specs: tuple[SkillSpec, ...]
    issues: tuple[SkillValidationIssue, ...]

    @property
    def has_blockers(self) -> bool:
        return any(issue.severity is IssueSeverity.BLOCKING for issue in self.issues)


class ResolvedToolRef(FrozenSkillModel):
    capability: str
    tool_id: str
    version: str
    content_hash: str = Field(min_length=64, max_length=64)


class ResolvedStep(FrozenSkillModel):
    step_id: str
    tool: ResolvedToolRef
    needs: tuple[str, ...]
    consumes: tuple[str, ...]
    produces: tuple[str, ...]
    parameter_names: tuple[str, ...]
    qc_gate: bool


class SkillLock(FrozenSkillModel):
    skill_id: str
    version: str
    content_hash: str = Field(min_length=64, max_length=64)


class SkillPlan(FrozenSkillModel):
    plan_id: str
    project_id: str
    dataset_ref: str
    input_manifest_hash: str = Field(min_length=64, max_length=64)
    input_artifact_id: str
    base_cfg_artifact_id: str | None
    preprocessing_parameters_hash: str | None
    skill_locks: tuple[SkillLock, ...]
    resolved_parameters: tuple[tuple[str, Any], ...]
    environment: EnvironmentSnapshot
    steps: tuple[ResolvedStep, ...]
    artifact_expectations: tuple[str, ...]
    qc_gates: tuple[str, ...]
    approval_requirements: tuple[str, ...]
    warnings: tuple[SkillValidationIssue, ...]
    plan_hash: str = Field(min_length=64, max_length=64)

    def approval_binding(self) -> tuple[str, str, str]:
        return self.plan_hash, self.input_manifest_hash, self.environment.environment_hash


def stable_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _topological_order(steps: tuple[SkillStep, ...]) -> tuple[str, ...]:
    remaining = {step.step_id: set(step.needs) for step in steps}
    ordered: list[str] = []
    while remaining:
        ready = sorted(step_id for step_id, needs in remaining.items() if not needs)
        if not ready:
            raise ValueError("Skill step graph contains a cycle")
        for step_id in ready:
            ordered.append(step_id)
            del remaining[step_id]
            for needs in remaining.values():
                needs.discard(step_id)
    return tuple(ordered)
