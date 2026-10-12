"""Agent contracts."""

from __future__ import annotations

from datetime import datetime
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, field_validator

from neuroagent.agent.models import AgentTaskRequest, GatewayResult, ModelProfile, RoutingDecision
from neuroagent.analysis.models import (
    AtlasPoint,
    ClusterLocalization,
    ClusterRecord,
    FailureDiagnosis,
    MlDesignRecommendation,
    MlTemplate,
    RoiExtractionRequest,
    RoiSignalRecord,
    RsFmriAnswer,
    TableInspection,
)

from .shared import StrictModel


class ModelProfileInput(ModelProfile):
    model_config = ConfigDict(extra="forbid", frozen=True)

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


class ModelProfileView(StrictModel):
    profile: ModelProfileInput
    version: int
    created_at: datetime


class ModelProfileCreate(StrictModel):
    model_config = ConfigDict(frozen=True)

    profile: ModelProfileInput
    api_key: str | None = None


class ModelListRequest(StrictModel):
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

    @field_validator("api_key_env")
    @classmethod
    def restrict_secret_environment_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.endswith("_API_KEY"):
            raise ValueError("api_key_env must end with _API_KEY")
        return value


class ModelListView(StrictModel):
    models: list[str]


class ProviderTestRequest(StrictModel):
    profile_id: str
    expected_profile_version: int = Field(ge=1)


class ProviderTestView(StrictModel):
    profile_id: str
    available: bool
    routing: RoutingDecision
    context_hash: str


class AgentTaskCreate(StrictModel):
    request: AgentTaskRequest
    expected_project_version: int = Field(ge=1)


class AgentTaskView(StrictModel):
    task_id: str
    project_id: str
    state: str
    result: GatewayResult
    created_at: datetime


class RunDiagnosisRequest(StrictModel):
    """A bounded log excerpt supplied by the UI for deterministic diagnosis."""

    log_text: str = Field(min_length=1, max_length=200_000)


class RunDiagnosisView(StrictModel):
    run_id: str | None = None
    diagnosis: FailureDiagnosis


class MlTableInspectRequest(StrictModel):
    project_id: str
    source_path: str
    max_rows: int = Field(default=100_000, ge=1, le=100_000)


class MlTableInspectView(StrictModel):
    project_id: str
    source_path_name: str
    inspection: TableInspection


class MlTemplateCreateRequest(StrictModel):
    design: MlDesignRecommendation
    source_filename: str = Field(
        default="features.csv",
        pattern=r"^[A-Za-z0-9_.-]+\.(csv|tsv|xlsx)$",
    )


class MlTemplateView(StrictModel):
    template: MlTemplate
    approval_required: bool = True


class RoiTableCreateRequest(StrictModel):
    design: RoiExtractionRequest
    records: tuple[RoiSignalRecord, ...] = Field(min_length=1, max_length=1_000_000)


class RoiTableView(StrictModel):
    valid: bool
    issues: tuple[str, ...]
    long_rows: tuple[dict[str, Any], ...] = ()
    wide_rows: tuple[dict[str, Any], ...] = ()


class ClusterLocalizationRequest(StrictModel):
    clusters: tuple[ClusterRecord, ...] = Field(min_length=1, max_length=100_000)
    atlas_points: tuple[AtlasPoint, ...] = ()
    max_distance_mm: float = Field(default=8, gt=0, le=100)


class ClusterLocalizationView(StrictModel):
    results: tuple[ClusterLocalization, ...]
    atlas_supplied: bool


class RsFmriQuestionRequest(StrictModel):
    question: str = Field(min_length=2, max_length=4_000)
    allow_remote_search: bool = False


class RsFmriAnswerView(StrictModel):
    answer: RsFmriAnswer
    remote_search_used: bool = False
