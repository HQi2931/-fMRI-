"""Server-owned scientific environment snapshots used for approval locks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from neuroagent.application.contracts import EnvironmentProbeView
from neuroagent.skills.models import EnvironmentSnapshot


@dataclass(frozen=True, slots=True)
class EnvironmentLock:
    snapshot: EnvironmentSnapshot
    probe: EnvironmentProbeView


class EnvironmentLockProvider(Protocol):
    def current(self) -> EnvironmentLock: ...
