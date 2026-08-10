"""Read-only loader for reviewed machine Skill specifications."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from neuroagent.skills.models import SkillSpec


class SkillLoadError(ValueError):
    pass


class SkillLoader:
    def load(self, source: Path, schema_source: Path | None = None) -> SkillSpec:
        path = source.resolve(strict=True)
        if path.name != "skill.yaml":
            raise SkillLoadError("machine Skill source must be named skill.yaml")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise SkillLoadError("skill.yaml must contain one mapping")
        if self._contains_forbidden_key(raw):
            raise SkillLoadError("Skill contains forbidden executable or state-transition fields")
        if schema_source is not None:
            schema = json.loads(schema_source.resolve(strict=True).read_text(encoding="utf-8"))
            errors = sorted(
                Draft202012Validator(schema).iter_errors(raw),
                key=lambda item: tuple(str(part) for part in item.absolute_path),
            )
            if errors:
                messages = "; ".join(error.message for error in errors)
                raise SkillLoadError(f"Skill schema validation failed: {messages}")
        try:
            return SkillSpec.model_validate(raw)
        except ValueError as exc:
            raise SkillLoadError(str(exc)) from exc

    def validate_parameters(self, parameters: Any, schema_source: Path) -> None:
        """Validate a runtime payload against its checked-in Skill schema."""

        schema = json.loads(schema_source.resolve(strict=True).read_text(encoding="utf-8"))
        errors = sorted(
            Draft202012Validator(schema).iter_errors(parameters),
            key=lambda item: tuple(str(part) for part in item.absolute_path),
        )
        if errors:
            messages = "; ".join(
                f"{'.'.join(str(part) for part in error.absolute_path) or '<root>'}: "
                f"{error.message}"
                for error in errors
            )
            raise SkillLoadError(f"Skill parameter schema validation failed: {messages}")

    def _contains_forbidden_key(self, value: Any) -> bool:
        forbidden = {
            "shell",
            "command",
            "matlab_script",
            "script_text",
            "code",
            "status_transition",
            "execute",
        }
        if isinstance(value, dict):
            return bool(forbidden.intersection(value)) or any(
                self._contains_forbidden_key(child) for child in value.values()
            )
        if isinstance(value, list):
            return any(self._contains_forbidden_key(child) for child in value)
        return False
