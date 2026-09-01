"""Fail-closed local secret lookup with process-environment precedence."""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import dotenv_values

_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]*_API_KEY$")


def write_env_secret(dotenv_path: Path, name: str, value: str) -> None:
    """Write one API key into the local ``.env`` file, replacing any existing line.

    The value never touches the database, logs, or audit payloads; it is written
    only to the untracked local dotenv file referenced by the profile's
    ``api_key_env``. Existing commented placeholders are upgraded in place.
    """

    if not _SECRET_NAME.fullmatch(name):
        raise ValueError(f"secret name must match the _API_KEY pattern: {name}")
    value = value.strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("secret value must be a single non-empty line")
    path = dotenv_path.expanduser().resolve()
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    pattern = re.compile(rf"^#?\s*{re.escape(name)}\s*=")
    replaced = False
    output: list[str] = []
    for line in lines:
        if pattern.match(line):
            output.append(f"{name}={value}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(f"{name}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


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
