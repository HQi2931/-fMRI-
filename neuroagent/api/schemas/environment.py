"""HTTP request schemas for local scientific environment configuration."""

from __future__ import annotations

from pydantic import Field

from neuroagent.api.schemas.shared import StrictHttpModel


class EnvironmentConfigUpdate(StrictHttpModel):
    """User-selected local scientific software locations.

    These values are local runtime configuration, not scientific parameters.
    The server validates the selected filesystem entries before persisting them;
    version labels are descriptive evidence and are never treated as exact
    compatibility claims.
    """

    matlab_executable: str | None = None
    spm_dir: str | None = None
    dpabi_dir: str | None = None
    matlab_version: str = Field(default="unspecified", min_length=1, max_length=120)
    spm_version: str = Field(default="unspecified", min_length=1, max_length=120)
    dpabi_version: str = Field(default="unspecified", min_length=1, max_length=120)
