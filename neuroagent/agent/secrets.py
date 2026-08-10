"""Narrow secret lookup port used by the model gateway."""

from __future__ import annotations

import os
from typing import Protocol


class SecretResolver(Protocol):
    def resolve(self, name: str) -> str | None: ...


class ProcessEnvironmentSecretResolver:
    """Resolve tests and explicitly exported secrets without caching their values."""

    def resolve(self, name: str) -> str | None:
        value = os.getenv(name)
        return value if value and value.strip() else None
