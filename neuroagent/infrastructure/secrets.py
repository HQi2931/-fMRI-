"""Fail-closed local secret lookup with process-environment precedence."""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import dotenv_values

_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]*_API_KEY$")


class LocalDotenvSecretResolver:
    """Read one requested API key without exporting or retaining dotenv contents."""

    def __init__(self, dotenv_path: Path) -> None:
        self._dotenv_path = dotenv_path.expanduser().resolve()

    def resolve(self, name: str) -> str | None:
        if not _SECRET_NAME.fullmatch(name):
            return None
        process_value = os.getenv(name)
        if process_value and process_value.strip():
            return process_value
        if not self._dotenv_path.is_file():
            return None
        value = dotenv_values(self._dotenv_path).get(name)
        return value if isinstance(value, str) and value.strip() else None
