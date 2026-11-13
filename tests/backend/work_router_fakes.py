"""Deterministic route adapter for tests; production routing is exercised separately."""

from __future__ import annotations

from types import SimpleNamespace

from neuroagent.application.contracts.conversations import ConversationWorkTaskView
from neuroagent.application.work_agents import work_agent_role_for_turn, work_agent_routing_question
from neuroagent.application.work_routing import (
    WorkAgentRouteChoice,
    WorkAgentRoutingContext,
    WorkAgentRoutingDecision,
    WorkAgentRoutingStatus,
)
from neuroagent.application.work_tools import PersistentModelCallBudget
from neuroagent.multi_agent.registry import WorkAgentRole


class FakeWorkAgentRouter:
    model = "test-work-router"

    async def choose(
        self,
        *,
        request_text: str,
        context: WorkAgentRoutingContext,
        persistent_model_budget: PersistentModelCallBudget | None,
    ) -> WorkAgentRoutingDecision:
        del persistent_model_budget
        selected = (
            (
                ConversationWorkTaskView(
                    agent_role=WorkAgentRole.ANALYSIS,
                    analysis_intent_task_id="selected-analysis-goal",
                    proposal_hash="a" * 64,
                ),
            )
            if context.has_selected_analysis_goal
            else ()
        )
        conversation = SimpleNamespace(
            foreground_agent_role=context.current_agent_role,
            messages=(),
            work_tasks=selected,
            active_run_id="bound-preprocessing-run"
            if context.has_bound_preprocessing_run
            else None,
        )
        if (
            context.required_initial_role is None
            and work_agent_routing_question(
                request_text,
                requested=None,
                conversation=conversation,
            )
            is not None
        ):
            return WorkAgentRoutingDecision(
                status=WorkAgentRoutingStatus.NEEDS_CLARIFICATION,
                role=None,
                choice=WorkAgentRouteChoice.CLARIFY,
                confidence=0.95,
                model=self.model,
                reason="model_clarify",
            )
        role = context.required_initial_role or work_agent_role_for_turn(
            request_text, requested=None, conversation=conversation
        )
        choice = (
            WorkAgentRouteChoice.ANALYSIS
            if role is WorkAgentRole.ANALYSIS
            else WorkAgentRouteChoice.PREPROCESSING
        )
        return WorkAgentRoutingDecision(
            status=WorkAgentRoutingStatus.ROUTED,
            role=role,
            choice=choice,
            confidence=0.99,
            model=self.model,
        )
