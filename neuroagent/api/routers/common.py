"""Shared transport dependencies for capability routers."""

from __future__ import annotations

from typing import Annotated, Literal, cast, overload

from fastapi import Header, Request

from neuroagent.application.ports import ApplicationCapabilityRegistryPort
from neuroagent.application.service_mixins.conversations import ConversationMixin
from neuroagent.application.service_mixins.environment import EnvironmentMixin
from neuroagent.application.service_mixins.external_imports import ExternalAnalysisImportMixin
from neuroagent.application.service_mixins.models import ModelAgentMixin
from neuroagent.application.service_mixins.plans import PlanApprovalMixin
from neuroagent.application.service_mixins.projects import ProjectDatasetMixin
from neuroagent.application.service_mixins.runs import RunMixin
from neuroagent.application.service_mixins.skills import SkillPlanMixin
from neuroagent.application.service_mixins.statistics import StatisticsMixin
from neuroagent.application.service_mixins.workspaces import WorkspaceMixin
from neuroagent.literature.service import LiteratureService
from neuroagent.memory.service import SemanticMemoryService

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)]


@overload
def capability_from(request: Request, capability: Literal["environment"]) -> EnvironmentMixin: ...


@overload
def capability_from(request: Request, capability: Literal["workspaces"]) -> WorkspaceMixin: ...


@overload
def capability_from(
    request: Request, capability: Literal["conversations"]
) -> ConversationMixin: ...


@overload
def capability_from(
    request: Request, capability: Literal["external_imports"]
) -> ExternalAnalysisImportMixin: ...


@overload
def capability_from(request: Request, capability: Literal["projects"]) -> ProjectDatasetMixin: ...


@overload
def capability_from(request: Request, capability: Literal["plans"]) -> PlanApprovalMixin: ...


@overload
def capability_from(request: Request, capability: Literal["skills"]) -> SkillPlanMixin: ...


@overload
def capability_from(request: Request, capability: Literal["statistics"]) -> StatisticsMixin: ...


@overload
def capability_from(request: Request, capability: Literal["runs"]) -> RunMixin: ...


@overload
def capability_from(request: Request, capability: Literal["models"]) -> ModelAgentMixin: ...


@overload
def capability_from(request: Request, capability: Literal["literature"]) -> LiteratureService: ...


@overload
def capability_from(
    request: Request, capability: Literal["semantic_memory"]
) -> SemanticMemoryService: ...


def capability_from(
    request: Request,
    capability: Literal[
        "environment",
        "workspaces",
        "conversations",
        "external_imports",
        "projects",
        "plans",
        "skills",
        "statistics",
        "runs",
        "models",
        "literature",
        "semantic_memory",
    ],
) -> object:
    """Resolve one explicitly composed application capability for a router."""

    registry = cast(ApplicationCapabilityRegistryPort, request.app.state.service)
    return registry.resolve_capability(capability)
