"""Public schemas for model routing and structured Agent recommendations."""

from __future__ import annotations

from enum import StrEnum
from ipaddress import ip_address
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelCapability(StrEnum):
    JSON_OBJECT = "json_object"
    STREAMING = "streaming"
    REASONING = "reasoning"
    WEB_SEARCH = "web_search"


class TaskType(StrEnum):
    INTENT_PARSER = "intent_parser"
    SKILL_PLANNER = "skill_planner"
    PLAN_EXPLAINER = "plan_explainer"
    LOG_SUMMARIZER = "log_summarizer"
    REPORT_WRITER = "report_writer"


class AgentSummaryPurpose(StrEnum):
    PROVIDER_CONNECTIVITY_TEST = "provider_connectivity_test"
    EXPLAIN_CURRENT_PLAN = "explain_current_plan"
    SUMMARIZE_REGISTERED_RUN = "summarize_registered_run"
    DRAFT_METHOD_REPORT = "draft_method_report"


class SafeAgentSummary(BaseModel):
    """Allowlisted, aggregate-only context that may leave the workstation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

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


class ModelProfile(BaseModel):
    """Non-secret model metadata. The API key is referenced by environment name."""

    model_config = ConfigDict(frozen=True)

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
        if parsed.scheme == "https":
            return normalized
        hostname = parsed.hostname
        if hostname == "localhost":
            return normalized
        try:
            is_loopback = "%" not in hostname and ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = False
        if is_loopback:
            return normalized
        raise ValueError("base_url must use HTTPS or an exact HTTP loopback host")


class AgentTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_type: TaskType
    project_id: str = Field(min_length=1, max_length=128)
    summary: SafeAgentSummary
    required_capabilities: frozenset[ModelCapability] = frozenset({ModelCapability.JSON_OBJECT})
    preferred_profile_id: str | None = None


class RedactedContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    payload: dict[str, Any]
    redaction_count: int = Field(ge=0)
    context_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class RoutingDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_type: TaskType
    selected_profile_id: str
    candidate_profile_ids: tuple[str, ...]
    required_capabilities: frozenset[ModelCapability]
    reason: str


class WorkspaceFormatAdvice(BaseModel):
    """Bounded, non-operational advice for a Work workspace format review."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=8_000)
    expected_layout: list[str] = Field(default_factory=list, max_length=100)
    adjustment_steps: list[str] = Field(default_factory=list, max_length=100)
    recommended_target_stage: str | None = Field(
        default=None,
        min_length=2,
        max_length=64,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]{1,63}$",
    )
    warnings: list[str] = Field(default_factory=list, max_length=100)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=100)
    requires_user_confirmation: bool


class StructuredRecommendation(WorkspaceFormatAdvice):
    """The only Agent result accepted by deterministic application services."""

    proposed_skill_request: dict[str, Any] | None = None


class ProviderResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str
    provider_request_id: str | None = None
    model: str
    usage: dict[str, int] = Field(default_factory=dict)
    citations: tuple[ProviderCitation, ...] = ()


class ProviderCitation(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    title: str


class GatewayResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    recommendation: StructuredRecommendation
    routing: RoutingDecision
    context_hash: str
    attempted_profile_ids: tuple[str, ...]
    model: str | None = None
    redaction_count: int = Field(default=0, ge=0)
