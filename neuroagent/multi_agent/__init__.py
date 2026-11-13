"""Permission-scoped Work Agent registry and coordination boundaries."""

from neuroagent.multi_agent.registry import (
    WORK_AGENT_REGISTRY,
    WorkAgentRole,
    WorkAgentSpec,
    resolve_work_agent,
    work_agent_allows_action,
)

__all__ = [
    "WORK_AGENT_REGISTRY",
    "WorkAgentRole",
    "WorkAgentSpec",
    "resolve_work_agent",
    "work_agent_allows_action",
]
