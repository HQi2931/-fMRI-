"""Agent contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic import ConfigDict, field_validator

from neuroagent.agent.models import GatewayResult, ModelProfile, RoutingDecision
from neuroagent.analysis.models import (
    ClusterLocalization,
    FailureDiagnosis,
    MlTemplate,
    RsFmriAnswer,
    TableInspection,
)
from neuroagent.api.schemas.shared import StrictHttpModel as StrictModel


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


class ModelListView(StrictModel):
    models: list[str]


class ProviderTestView(StrictModel):
    profile_id: str
    available: bool
    routing: RoutingDecision
    context_hash: str


class AgentTaskView(StrictModel):
    task_id: str
    project_id: str
    state: str
    result: GatewayResult
    created_at: datetime


class RunDiagnosisView(StrictModel):
    run_id: str | None = None
    diagnosis: FailureDiagnosis


class MlTableInspectView(StrictModel):
    project_id: str
    source_path_name: str
    inspection: TableInspection


class MlTemplateView(StrictModel):
    template: MlTemplate
    approval_required: bool = True


class RoiTableView(StrictModel):
    valid: bool
    issues: tuple[str, ...]
    long_rows: tuple[dict[str, Any], ...] = ()
    wide_rows: tuple[dict[str, Any], ...] = ()


class ClusterLocalizationView(StrictModel):
    results: tuple[ClusterLocalization, ...]
    atlas_supplied: bool


class RsFmriAnswerView(StrictModel):
    answer: RsFmriAnswer
    remote_search_used: bool = False
