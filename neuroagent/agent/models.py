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


class StructuredRecommendation(BaseModel):
    """The only Agent result accepted by deterministic application services."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=8_000)
    proposed_skill_request: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list, max_length=100)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=100)
    requires_user_confirmation: bool


class ProviderResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str
    provider_request_id: str | None = None
    model: str
    usage: dict[str, int] = Field(default_factory=dict)


class GatewayResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    recommendation: StructuredRecommendation
    routing: RoutingDecision
    context_hash: str
    attempted_profile_ids: tuple[str, ...]
