"""Compile validated Skill specifications into a stable, immutable plan."""

from __future__ import annotations

from typing import Any, Protocol

from neuroagent.skills.models import (
    ResolvedStep,
    ResolvedToolRef,
    SkillLock,
    SkillPlan,
    SkillResolution,
    SkillStep,
    stable_hash,
)
from neuroagent.skills.validation import SkillValidator


class CapabilityRegistry(Protocol):
    def resolve_capability(self, capability: str) -> ResolvedToolRef: ...


class SkillCompileError(ValueError):
    pass


class SkillCompiler:
    def __init__(self, tools: CapabilityRegistry, validator: SkillValidator) -> None:
        self._tools = tools
        self._validator = validator

    def compile(self, resolution: SkillResolution) -> SkillPlan:
        report = self._validator.validate_resolution(resolution)
        if report.has_blockers:
            codes = sorted(
                issue.code for issue in report.issues if issue.severity.value == "blocking"
            )
            raise SkillCompileError(f"Skill resolution has blocking issues: {codes}")

        merged: dict[str, SkillStep] = {}
        for spec in resolution.selected_specs:
            for step in spec.steps:
                existing = merged.get(step.step_id)
                if existing is not None and existing != step:
                    raise SkillCompileError(
                        f"public step {step.step_id!r} has incompatible definitions"
                    )
                merged[step.step_id] = step
        compiled_steps = _infer_artifact_dependencies(tuple(merged.values()))
        merged = {step.step_id: step for step in compiled_steps}
        order = _topological_order(compiled_steps)
        resolved_steps = tuple(
            ResolvedStep(
                step_id=step_id,
                tool=self._tools.resolve_capability(merged[step_id].capability),
                needs=tuple(sorted(merged[step_id].needs)),
                consumes=merged[step_id].consumes,
                produces=merged[step_id].produces,
                parameter_names=merged[step_id].parameter_names,
                qc_gate=merged[step_id].qc_gate,
            )
            for step_id in order
        )
        request = resolution.request
        parameters = _resolved_parameters(request)
        preprocessing_hash = (
            stable_hash(request.preprocessing.model_dump(mode="json"))
            if request.preprocessing is not None
            else None
        )
        locks = tuple(
            SkillLock(
                skill_id=spec.skill_id,
                version=spec.version,
                content_hash=spec.content_hash,
            )
            for spec in sorted(resolution.selected_specs, key=lambda item: item.skill_id)
        )
        body: dict[str, Any] = {
            "project_id": request.project_id,
            "dataset_ref": request.dataset_ref,
            "input_manifest_hash": request.input_manifest_hash,
            "input_artifact_id": request.input_artifact.artifact_id,
            "base_cfg_artifact_id": request.base_cfg_artifact_id,
            "preprocessing_parameters_hash": preprocessing_hash,
            "skill_locks": [item.model_dump(mode="json") for item in locks],
            "resolved_parameters": parameters,
            "environment": resolution.environment.model_dump(mode="json"),
            "steps": [item.model_dump(mode="json") for item in resolved_steps],
            "artifact_expectations": sorted(
                {
                    *(f"primary:{name}" for name in request.primary_outputs),
                    *(
                        contract.artifact_type
                        for spec in resolution.selected_specs
                        for contract in spec.output_artifacts
                    ),
                }
            ),
            "qc_gates": sorted(
                {gate for spec in resolution.selected_specs for gate in spec.qc_requirements}
            ),
            "approval_requirements": [
                "plan_hash",
                "input_manifest_hash",
                "environment_hash",
                "scientific_parameters",
            ],
        }
        plan_hash = stable_hash(body)
        warnings = tuple(issue for issue in report.issues if issue.severity.value != "blocking")
        return SkillPlan(
            plan_id=f"plan-{plan_hash[:16]}",
            project_id=request.project_id,
            dataset_ref=request.dataset_ref,
            input_manifest_hash=request.input_manifest_hash,
            input_artifact_id=request.input_artifact.artifact_id,
            base_cfg_artifact_id=request.base_cfg_artifact_id,
            preprocessing_parameters_hash=preprocessing_hash,
            skill_locks=locks,
            resolved_parameters=tuple(parameters),
            environment=resolution.environment,
            steps=resolved_steps,
            artifact_expectations=tuple(body["artifact_expectations"]),
            qc_gates=tuple(body["qc_gates"]),
            approval_requirements=tuple(body["approval_requirements"]),
            warnings=warnings,
            plan_hash=plan_hash,
        )


def _resolved_parameters(request: Any) -> list[tuple[str, Any]]:
    values: list[tuple[str, Any]] = []
    if request.preprocessing is not None:
        values.append(("preprocessing", request.preprocessing.model_dump(mode="json")))
    if request.base_cfg_artifact_id is not None:
        values.append(("base_cfg_artifact_id", request.base_cfg_artifact_id))
    if request.alff_falff is not None:
        values.append(("alff_falff", request.alff_falff.model_dump(mode="json")))
    if request.reho is not None:
        values.append(("reho", request.reho.model_dump(mode="json")))
    return sorted(values, key=lambda item: item[0])


def _infer_artifact_dependencies(steps: tuple[SkillStep, ...]) -> tuple[SkillStep, ...]:
    producers: dict[str, list[str]] = {}
    for step in steps:
        for artifact in step.produces:
            producers.setdefault(artifact, []).append(step.step_id)
    compiled: list[SkillStep] = []
    for step in steps:
        inferred = set(step.needs)
        for artifact in step.consumes:
            candidates = [item for item in producers.get(artifact, ()) if item != step.step_id]
            if len(candidates) > 1:
                raise SkillCompileError(
                    f"artifact {artifact!r} has ambiguous producers: {sorted(candidates)}"
                )
            inferred.update(candidates)
        compiled.append(step.model_copy(update={"needs": tuple(sorted(inferred))}))
    return tuple(compiled)


def _topological_order(steps: tuple[SkillStep, ...]) -> tuple[str, ...]:
    remaining = {step.step_id: set(step.needs) for step in steps}
    order: list[str] = []
    while remaining:
        ready = sorted(step_id for step_id, dependencies in remaining.items() if not dependencies)
        if not ready:
            raise SkillCompileError("compiled Skill graph contains a cycle")
        for step_id in ready:
            order.append(step_id)
            del remaining[step_id]
            for dependencies in remaining.values():
                dependencies.discard(step_id)
    return tuple(order)
