"""Transport-owned request DTOs for Agent and model-provider endpoints."""

from __future__ import annotations

from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, field_validator

from neuroagent.agent.models import AgentSummaryPurpose, ModelCapability, TaskType
from neuroagent.api.schemas.shared import StrictHttpModel


class SafeAgentSummary(StrictHttpModel):
    """Allowlisted, aggregate-only context that may leave the workstation."""

    purpose: AgentSummaryPurpose
    metric_kinds: frozenset[Literal["alff", "falff", "reho"]] = frozenset()
    workflow_state: Literal[
        "not_started",
        "draft",
        "awaiting_approval",
        "approved",
        "queued",
        "running",
        "qc_review",
        "succeeded",
        "failed",
        "cancelled",
    ] = "not_started"
    issue_count: int = Field(default=0, ge=0, le=100_000)
    has_blocking_issues: bool = False
    format_issues: tuple[str, ...] = Field(default=(), max_length=50)
    workspace_kind: str | None = Field(default=None, max_length=32)
    input_stage: str | None = Field(default=None, max_length=32)
    file_count: int = Field(default=0, ge=0, le=100_000)
    nifti_count: int = Field(default=0, ge=0, le=100_000)
    dicom_count: int = Field(default=0, ge=0, le=100_000)
    subject_count: int = Field(default=0, ge=0, le=100_000)
    functional_subject_count: int = Field(default=0, ge=0, le=100_000)
    anatomical_subject_count: int = Field(default=0, ge=0, le=100_000)
    output_directories: tuple[str, ...] = Field(default=(), max_length=100)
    user_question: str | None = Field(default=None, max_length=2_000)


class AgentTaskRequest(StrictHttpModel):
    task_type: TaskType
    project_id: str = Field(min_length=1, max_length=128)
    summary: SafeAgentSummary
    required_capabilities: frozenset[ModelCapability] = frozenset({ModelCapability.JSON_OBJECT})
    preferred_profile_id: str | None = None


class ModelProfileInput(StrictHttpModel):
    """Validated public profile shape (secrets are provided separately)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    provider: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    base_url: str
    model: str = Field(min_length=1, max_length=200)
    api_key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,127}$")
    priority: int = Field(default=100, ge=0, le=10_000)
    capabilities: frozenset[ModelCapability] = frozenset()
    timeout_seconds: float = Field(default=45.0, gt=0, le=300)
    context_window_tokens: int = Field(default=16_384, ge=4_096, le=2_000_000)
    max_output_tokens: int = Field(default=2_048, ge=256, le=65_536)
    thinking_mode: Literal["provider_default", "disabled"] = "provider_default"

    @field_validator("api_key_env")
    @classmethod
    def restrict_secret_environment_name(cls, value: str) -> str:
        if not value.endswith("_API_KEY"):
            raise ValueError("api_key_env must end with _API_KEY")
        return value

    @field_validator("base_url")
    @classmethod
    def reject_secret_bearing_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain credentials, query, or fragment")
        return value


class ModelProfileCreate(StrictHttpModel):
    model_config = ConfigDict(frozen=True)

    profile: ModelProfileInput
    api_key: str | None = None


class ModelListRequest(StrictHttpModel):
    model_config = ConfigDict(frozen=True)

    base_url: str
    api_key: str | None = None
    api_key_env: str | None = None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlsplit(normalized)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("base_url contains an invalid port") from exc
        if port == 0:
            raise ValueError("base_url contains an invalid port")
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "base_url must be an HTTP(S) URL without credentials, query, or fragment"
            )
        if parsed.scheme == "https" or parsed.hostname == "localhost":
            return normalized
        try:
            is_loopback = "%" not in parsed.hostname and ip_address(parsed.hostname).is_loopback
        except ValueError:
            is_loopback = False
        if is_loopback:
            return normalized
        raise ValueError("base_url must use HTTPS or an exact HTTP loopback host")

    @field_validator("api_key_env")
    @classmethod
    def restrict_secret_environment_name(cls, value: str | None) -> str | None:
        if value is not None and not value.endswith("_API_KEY"):
            raise ValueError("api_key_env must end with _API_KEY")
        return value


class ProviderTestRequest(StrictHttpModel):
    profile_id: str
    expected_profile_version: int = Field(ge=1)


class RsFmriQuestionRequest(StrictHttpModel):
    question: str = Field(min_length=2, max_length=4_000)
    allow_remote_search: bool = False


class AgentTaskCreate(StrictHttpModel):
    request: AgentTaskRequest
    expected_project_version: int = Field(ge=1)
