"""Server-owned scientific environment snapshots used for approval locks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from neuroagent.application.contracts import (
    EnvironmentConfigUpdate,
    EnvironmentConfigView,
    EnvironmentProbeView,
)
from neuroagent.skills.models import EnvironmentSnapshot


@dataclass(frozen=True, slots=True)
class EnvironmentConfiguration:
    """Effective local software configuration used by API and Worker."""

    matlab_executable: Path | None
    spm_dir: Path | None
    dpabi_dir: Path | None
    matlab_version: str
    spm_version: str
    dpabi_version: str
    adapter_version: str


@dataclass(frozen=True, slots=True)
class EnvironmentLock:
    snapshot: EnvironmentSnapshot
    probe: EnvironmentProbeView
    configuration: EnvironmentConfiguration | None = None


class EnvironmentLockProvider(Protocol):
    def current(self) -> EnvironmentLock: ...

    def configuration(self) -> EnvironmentConfiguration: ...

    def configuration_view(self) -> EnvironmentConfigView: ...

    def update_configuration(self, request: EnvironmentConfigUpdate) -> EnvironmentConfigView: ...
