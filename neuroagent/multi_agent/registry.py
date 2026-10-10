"""Role contracts for the two Work agents; no model runtime or side effects."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field


class WorkAgentRole(StrEnum):
    PREPROCESSING = "preprocessing"
    ANALYSIS = "analysis"


class WorkAgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: WorkAgentRole
    display_name: str = Field(min_length=1)
    prompt_key: str = Field(min_length=1)
    draft_key: str = Field(min_length=1)
    tool_catalog: str = Field(min_length=1)
    model_actions: tuple[str, ...]


WORK_AGENT_REGISTRY: Mapping[WorkAgentRole, WorkAgentSpec] = MappingProxyType(
    {
        WorkAgentRole.PREPROCESSING: WorkAgentSpec(
            role=WorkAgentRole.PREPROCESSING,
            display_name="预处理 Agent",
            prompt_key="work_preprocessing",
            draft_key="work_draft",
            tool_catalog="preprocessing",
            model_actions=(
                "conversation",
                "get_progress",
                "get_qc_status",
                "get_statistical_results",
                "prepare_preprocessing_plan",
                "propose_workspace_changes",
                "propose_run_confirmation",
            ),
        ),
        WorkAgentRole.ANALYSIS: WorkAgentSpec(
            role=WorkAgentRole.ANALYSIS,
            display_name="数据分析 Agent",
            prompt_key="work_analysis",
            draft_key="analysis_draft",
            tool_catalog="analysis",
            model_actions=(
                "conversation",
                "get_progress",
                "get_qc_status",
                "get_statistical_results",
                "prepare_statistical_analysis",
                "request_preprocessing_handoff",
            ),
        ),
    }
)


def resolve_work_agent(role: WorkAgentRole | str) -> WorkAgentSpec:
    return WORK_AGENT_REGISTRY[WorkAgentRole(role)]


def work_agent_allows_action(role: WorkAgentRole | str, action: str) -> bool:
    return action in resolve_work_agent(role).model_actions
