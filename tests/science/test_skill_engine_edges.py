from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from neuroagent.domain.fmri import ArtifactLineage, MetricKind
from neuroagent.domain.fmri.skillpacks.builtin import build_builtin_registry, builtin_skill_specs
from neuroagent.skills import (
    EnvironmentSnapshot,
    SkillCompileError,
    SkillCompiler,
    SkillLoader,
    SkillLoadError,
    SkillRegistry,
    SkillRegistryError,
    SkillRequest,
    SkillResolver,
    SkillValidator,
)
from neuroagent.skills.models import (
    SkillSpec,
    SkillStatus,
    SkillStep,
)
from neuroagent.tools import ToolRegistry, build_default_tool_registry
from tests.science.test_skill_compiler import environment, request


def test_loader_rejects_wrong_name_non_mapping_forbidden_and_schema_error(tmp_path: Path) -> None:
    loader = SkillLoader()
    wrong = tmp_path / "wrong.yaml"
    wrong.write_text("[]", encoding="utf-8")
    with pytest.raises(SkillLoadError, match=r"named skill\.yaml"):
        loader.load(wrong)

    source = tmp_path / "skill.yaml"
    source.write_text("[]", encoding="utf-8")
    with pytest.raises(SkillLoadError, match="one mapping"):
        loader.load(source)

    source.write_text("command: whoami", encoding="utf-8")
    with pytest.raises(SkillLoadError, match="forbidden"):
        loader.load(source)

    source.write_text("schema_version: '1.0'", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": "object", "required": ["missing"]}), encoding="utf-8")
    with pytest.raises(SkillLoadError, match="schema validation"):
        loader.load(source, schema)

    schema.write_text(json.dumps({"type": "object"}), encoding="utf-8")
    with pytest.raises(SkillLoadError):
        loader.load(source, schema)


def test_runtime_parameter_schema_validation_blocks_drift() -> None:
    validator = SkillValidator()
    report = validator.validate_parameter_payload(
        "plan-alff-falff",
        {
            "tr_seconds": 2.0,
            "low_frequency_hz": 0.01,
            "high_frequency_hz": 0.08,
        },
    )
    assert report.has_blockers
    assert report.issues[0].code == "PARAMETER_SCHEMA_VALIDATION_FAILED"

    qc_report = validator.validate_parameter_payload(
        "review-rsfmri-qc",
        {
            "input_manifest_hash": "a" * 64,
            "subject_order": [],
            "metric_artifact_ids": [],
            "checks": [],
        },
    )
    assert qc_report.has_blockers


def test_registry_duplicate_freeze_deprecation_and_unknown_paths() -> None:
    spec = builtin_skill_specs()[0]
    registry = SkillRegistry()
    registry.register(spec)
    with pytest.raises(SkillRegistryError, match="duplicate"):
        registry.register(spec)
    deprecated = spec.model_copy(update={"version": "0.9.0", "status": SkillStatus.DEPRECATED})
    registry.register(deprecated)
    assert len(registry.list()) == 1
    assert len(registry.list(include_deprecated=True)) == 2
    assert registry.resolve(spec.skill_id).version == "1.0.0"
    assert registry.resolve(spec.skill_id, "0.9.0").status is SkillStatus.DEPRECATED
    with pytest.raises(SkillRegistryError, match="unknown"):
        registry.resolve("unknown", "1.0.0")
    with pytest.raises(SkillRegistryError, match="no reviewed"):
        registry.resolve("unknown")
    registry.freeze()
    with pytest.raises(SkillRegistryError, match="frozen"):
        registry.register(spec.model_copy(update={"version": "1.0.1"}))


@pytest.mark.parametrize(
    ("metrics", "skill_id"),
    [
        ((MetricKind.ALFF,), "rsfmri.metric.alff_falff"),
        ((MetricKind.FALFF,), "rsfmri.metric.alff_falff"),
        ((MetricKind.REHO,), "rsfmri.metric.reho"),
    ],
)
def test_resolver_selects_single_metric_protocols(
    base_lineage: ArtifactLineage, metrics: tuple[MetricKind, ...], skill_id: str
) -> None:
    original = request(base_lineage)
    values = original.model_dump()
    values["requested_metrics"] = metrics
    if metrics == (MetricKind.REHO,):
        values["alff_falff"] = None
    else:
        values["reho"] = None
    resolved = SkillResolver(build_builtin_registry()).resolve(
        SkillRequest.model_validate(values), environment()
    )
    assert resolved.selected_specs[0].skill_id == skill_id


def test_resolver_reports_missing_reviewed_skill(base_lineage: ArtifactLineage) -> None:
    empty = SkillRegistry()
    empty.freeze()
    resolved = SkillResolver(empty).resolve(request(base_lineage), environment())
    assert resolved.has_blockers
    assert resolved.issues[0].code == "SKILL_NOT_AVAILABLE"


def test_validator_checks_every_environment_lock(base_lineage: ArtifactLineage) -> None:
    resolved = SkillResolver(build_builtin_registry()).resolve(
        request(base_lineage),
        EnvironmentSnapshot(
            matlab_version="R2024a",
            spm_version="SPM8",
            dpabi_version="V9.0",
            adapter_version="2.0.0",
            environment_hash="x" * 64,
        ),
    )
    codes = {issue.code for issue in SkillValidator().validate_resolution(resolved).issues}
    assert codes == {
        "MATLAB_VERSION_INCOMPATIBLE",
        "SPM_VERSION_INCOMPATIBLE",
        "DPABI_VERSION_INCOMPATIBLE",
        "ADAPTER_VERSION_INCOMPATIBLE",
    }


def test_compiler_blocks_missing_tool_capability(base_lineage: ArtifactLineage) -> None:
    resolved = SkillResolver(build_builtin_registry()).resolve(request(base_lineage), environment())
    tools = ToolRegistry()
    tools.freeze()
    with pytest.raises(ValueError, match="no registered Tool"):
        SkillCompiler(tools, SkillValidator()).compile(resolved)


def test_skill_model_rejects_self_dependency_missing_dependency_and_cycle() -> None:
    with pytest.raises(ValidationError, match="cannot depend on itself"):
        SkillStep(step_id="self", capability="cap", needs=("self",))

    base = {
        "schema_version": "1.0",
        "skill_id": "test.skill",
        "version": "1.0.0",
        "title": "Test",
        "status": "reviewed",
        "requested_metrics": (),
        "required_parameters": (),
        "input_artifacts": (),
        "output_artifacts": (),
        "required_capabilities": ("cap",),
        "workflow_template_ref": "workflow",
        "qc_requirements": (),
        "compatibility": {
            "matlab": "R2023b",
            "spm": "SPM12",
            "dpabi": "V8.2_240510",
            "adapter": "1.0.0",
        },
        "evidence_refs": ("evidence",),
        "known_limitations": (),
        "reviewed_by": ("reviewer",),
    }
    with pytest.raises(ValidationError, match="missing step"):
        SkillSpec.model_validate(
            {**base, "steps": ({"step_id": "a", "capability": "cap", "needs": ("b",)},)}
        )
    with pytest.raises(ValidationError, match="cycle"):
        SkillSpec.model_validate(
            {
                **base,
                "steps": (
                    {"step_id": "a", "capability": "cap", "needs": ("b",)},
                    {"step_id": "b", "capability": "cap", "needs": ("a",)},
                ),
            }
        )
    with pytest.raises(ValidationError, match="undeclared"):
        SkillSpec.model_validate(
            {
                **base,
                "steps": ({"step_id": "a", "capability": "other", "needs": ()},),
            }
        )


def test_reviewed_skill_requires_reviewer_and_evidence() -> None:
    spec = builtin_skill_specs()[0]
    values = spec.model_dump()
    values["reviewed_by"] = ()
    with pytest.raises(ValidationError, match="reviewer"):
        SkillSpec.model_validate(values)
    no_evidence = spec.model_copy(update={"evidence_refs": ()})
    report = SkillValidator().validate_spec(no_evidence)
    assert report.has_blockers

    scientific = builtin_skill_specs()[0].model_copy(
        update={"reviewed_by": ("skill_workflow_engineer", "matlab_dpabi_engineer")}
    )
    report = SkillValidator().validate_spec(scientific)
    assert {issue.code for issue in report.issues} == {
        "SCIENTIFIC_SKILL_WITHOUT_METHODOLOGIST_REVIEW"
    }


def test_request_rejects_missing_parameters_and_manifest_mismatch(
    base_lineage: ArtifactLineage,
) -> None:
    values = request(base_lineage).model_dump()
    values["alff_falff"] = None
    with pytest.raises(ValidationError, match="require alff_falff"):
        SkillRequest.model_validate(values)
    values = request(base_lineage).model_dump()
    values["input_manifest_hash"] = "z" * 64
    with pytest.raises(ValidationError, match="lineage"):
        SkillRequest.model_validate(values)


def test_conflicting_public_steps_are_not_silently_merged(
    base_lineage: ArtifactLineage,
) -> None:
    spec = builtin_skill_specs()[1]
    changed_step = spec.steps[0].model_copy(update={"produces": ("different",)})
    changed = spec.model_copy(update={"skill_id": "test.changed", "steps": (changed_step,)})
    resolution = SkillResolver(build_builtin_registry()).resolve(
        request(base_lineage), environment()
    )
    resolution = resolution.model_copy(update={"selected_specs": (spec, changed)})
    with pytest.raises(SkillCompileError, match="incompatible definitions"):
        SkillCompiler(build_default_tool_registry(), SkillValidator()).compile(resolution)
