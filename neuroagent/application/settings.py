"""Local-first runtime settings loaded from the repository ``.env`` file.

Secrets are deliberately absent from defaults. Scientific software paths and
allowed filesystem roots must be configured locally with ``RSFMRI_*`` values.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _parse_paths(value: Any) -> list[Path]:
    if isinstance(value, str):
        return [Path(item.strip()).expanduser() for item in value.split(os.pathsep) if item.strip()]
    if isinstance(value, (list, tuple)):
        return [Path(item).expanduser() for item in value]
    raise ValueError("allowed_source_roots must be a path-separated string or a list")


PathList = Annotated[list[Path], NoDecode, BeforeValidator(_parse_paths)]


class Settings(BaseSettings):
    """Validated non-secret configuration for API and Worker processes."""

    model_config = SettingsConfigDict(
        env_prefix="RSFMRI_",
        env_file=_PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    database_url: str = "sqlite:///work/neuroagent.db"
    host: Literal["127.0.0.1", "localhost", "::1"] = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65_535)
    allowed_source_roots: PathList = Field(
        default_factory=lambda: [_PROJECT_ROOT / "data", _PROJECT_ROOT / "tests" / "fixtures"]
    )
    allowed_work_root: Path = _PROJECT_ROOT / "work"
    matlab_executable: Path | None = None
    spm_dir: Path | None = None
    dpabi_dir: Path | None = None
    # Version labels are user-supplied evidence, not compatibility promises.
    matlab_version: str = "unspecified"
    spm_version: str = "unspecified"
    dpabi_version: str = "unspecified"
    adapter_version: str = "1.0.0"
    dataset_scan_max_files: int = Field(default=100_000, ge=1)
    worker_lease_seconds: int = Field(default=30, ge=1)
    idempotency_lease_seconds: int = Field(default=300, ge=30)
    redaction_salt: str | None = None
    secrets_file: Path = _PROJECT_ROOT / ".env"
    environment: str = "development"
    serve_frontend: bool = False
    frontend_dist: Path = _PROJECT_ROOT / "web" / "dist"
    enable_real_execution: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        """Load settings from process environment and the repository root ``.env``."""

        return cls()
