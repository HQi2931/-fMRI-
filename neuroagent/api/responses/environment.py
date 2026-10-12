"""Environment contracts."""

from __future__ import annotations

from neuroagent.api.schemas.shared import StrictHttpModel as StrictModel


class EnvironmentComponent(StrictModel):
    name: str
    available: bool
    evidence: str | None = None


class EnvironmentProbeView(StrictModel):
    ready: bool
    environment_hash: str
    components: list[EnvironmentComponent]


class EnvironmentConfigView(StrictModel):
    matlab_executable: str | None = None
    spm_dir: str | None = None
    dpabi_dir: str | None = None
    matlab_version: str
    spm_version: str
    dpabi_version: str
    configured: bool


class HealthView(StrictModel):
    status: str = "ok"
    database: str = "ok"
