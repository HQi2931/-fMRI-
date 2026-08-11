from __future__ import annotations

import json
from pathlib import Path

EXTENSION_SKILLS = (
    "diagnose-dpabi-failure",
    "extract-roi-signals",
    "organize-dpabi-input",
    "prepare-demographics-template",
    "inspect-ml-table",
    "design-tabular-ml",
    "run-tabular-ml",
    "analyze-ml-results",
    "localize-statistical-clusters",
    "answer-rsfmri-methodology",
)


def test_extension_skill_packages_are_complete() -> None:
    root = Path(__file__).resolve().parents[2] / "skills"
    for skill in EXTENSION_SKILLS:
        package = root / skill
        assert (package / "SKILL.md").is_file()
        assert (package / "skill.yaml").is_file()
        assert (package / "agents" / "openai.yaml").is_file()
        schema = json.loads((package / "parameters.schema.json").read_text(encoding="utf-8"))
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
