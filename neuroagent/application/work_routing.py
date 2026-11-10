"""Contracts for model-assisted automatic Work Agent selection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from neuroagent.application.work_tools import PersistentModelCallBudget
from neuroagent.multi_agent.registry import WorkAgentRole


class WorkAgentRoutingStatus(StrEnum):
    ROUTED = "routed"
    NEEDS_CLARIFICATION = "needs_clarification"


class WorkAgentRouteChoice(StrEnum):
    PREPROCESSING = "preprocessing"
    ANALYSIS = "analysis"
    CLARIFY = "clarify"


@dataclass(frozen=True)
class WorkAgentRoutingContext:
    """Minimal non-file context allowed in a route-only request."""

    current_agent_role: WorkAgentRole
    has_project: bool
    has_bound_preprocessing_run: bool
    has_selected_analysis_goal: bool
    required_initial_role: WorkAgentRole | None = None


@dataclass(frozen=True)
class WorkAgentRoutingDecision:
    status: WorkAgentRoutingStatus
    role: WorkAgentRole | None
    choice: WorkAgentRouteChoice
    confidence: float | None
    model: str
    reason: str | None = None
    source: str = "model"
    profile_id: str | None = None


class WorkAgentRouter(Protocol):
    async def choose(
        self,
        *,
        request_text: str,
        context: WorkAgentRoutingContext,
        persistent_model_budget: PersistentModelCallBudget | None,
    ) -> WorkAgentRoutingDecision: ...


WorkAgentGenerate = Callable[..., Awaitable[Any]]


class WorkAgentRouterFactory(Protocol):
    """Construct the model-backed router outside the application layer.

    The application package must not import infrastructure adapters. The
    composition root supplies a factory that binds its provider-calling router
    to the application's own generate callable.
    """

    def __call__(self, generate: WorkAgentGenerate) -> WorkAgentRouter: ...
