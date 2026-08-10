"""Load non-secret model profiles from a local JSON configuration."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from neuroagent.agent.models import ModelProfile


class ModelConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profiles: list[ModelProfile]
    routes: dict[str, list[str]]


def load_model_configuration(path: Path) -> ModelConfiguration:
    data = json.loads(path.read_text(encoding="utf-8"))
    return ModelConfiguration.model_validate(data)
