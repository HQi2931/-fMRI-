from __future__ import annotations

import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from neuroagent.domain.fmri.skillpacks.builtin import builtin_skill_specs
from neuroagent.skills import SkillLoader

SKILLS = (
    "inspect-rsfmri-dataset",
    "plan-dpabi-preprocessing",
    "plan-alff-falff",
    "plan-reho",
    "review-rsfmri-qc",
    "plan-rsfmri-statistics",
)


def test_project_skill_packages_have_codex_and_machine_contracts() -> None:
    root = Path("skills")
    loader = SkillLoader()
    for name in SKILLS:
        skill_root = root / name
        assert (skill_root / "SKILL.md").is_file()
        assert (skill_root / "agents" / "openai.yaml").is_file()
        spec = loader.load(skill_root / "skill.yaml")
        assert spec.status.value == "reviewed"
        schema = json.loads((skill_root / "parameters.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        interface = yaml.safe_load(
            (skill_root / "agents" / "openai.yaml").read_text(encoding="utf-8")
        )
        assert f"${name}" in interface["interface"]["default_prompt"]


def test_checked_in_skill_specs_match_builtins() -> None:
    builtins = {spec.skill_id: spec for spec in builtin_skill_specs()}
    loader = SkillLoader()
    for name in SKILLS:
        disk = loader.load(Path("skills") / name / "skill.yaml")
        assert disk == builtins[disk.skill_id]
