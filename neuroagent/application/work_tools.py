"""Work-side tool catalog and loop driver.

Work already receives run/QC/plan state through ``work_context``, so this catalog
deliberately does **not** re-expose it.  What it adds is the state that context
does not carry: statistical correction capabilities, the registered artifact
types of a run, and the reviewed Skill list.  All of it is Tier A (read only).

The loop's final text is the same JSON contract Work always used, so parsing,
validation and the whole authorization path stay untouched.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from neuroagent.agent.gateway import ChatMessages, ToolTurnResult
from neuroagent.agent.loop import (
    UNTRUSTED_TOOL_RESULT_INSTRUCTION,
    AgentLoop,
    AgentLoopBudget,
    ToolInvoker,
)
from neuroagent.agent.models import ProviderResponse
from neuroagent.agent.redaction import OutboundContextPolicy
from neuroagent.agent.tool_result import ToolCallOutcome
from neuroagent.agent.tool_spec import LlmTool, ToolTier
from neuroagent.application.agent_tools import AuditSink, ToolBinding, ToolDispatcher
from neuroagent.application.errors import NotFoundError

logger = logging.getLogger(__name__)

MAX_ARTIFACTS_REPORTED = 50
MAX_SKILLS_REPORTED = 50
MAX_CORRECTIONS_REPORTED = 20

GetRunCall = Callable[[str], Mapping[str, Any]]
ListArtifactsCall = Callable[[str], Sequence[Mapping[str, Any]]]
ListResultsCall = Callable[[str], Sequence[Mapping[str, Any]]]
CorrectionCapabilitiesCall = Callable[[], Sequence[Mapping[str, Any]]]
ListSkillsCall = Callable[[], Sequence[Mapping[str, Any]]]


@dataclass(frozen=True)
class AgentExecutionContext:
    """Request-scoped authority passed to model-visible Work tools.

    The model may name any run-shaped identifier, but the server decides which
    identifiers are actually readable for this turn.  Keeping this object
    immutable makes it safe to hand to a dispatcher created for one request.
    """

    conversation_id: str | None = None
    project_id: str | None = None
    allowed_run_ids: frozenset[str] = frozenset()
    selected_profile_id: str | None = None
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None

GET_RUN_STATUS_TOOL = LlmTool(
    name="get_run_status",
    description=(
        "Read the current state of a run by its run_id, including stage, attempt, "
        "QC status and artifact count."
    ),
    parameters={
        "type": "object",
        "properties": {"run_id": {"type": "string", "minLength": 1, "maxLength": 64}},
        "required": ["run_id"],
        "additionalProperties": False,
    },
    tier=ToolTier.READ,
)

LIST_RUN_ARTIFACTS_TOOL = LlmTool(
    name="list_run_artifacts",
    description=(
        "List the registered artifact types and counts for a run. Use this before "
        "proposing a statistical analysis so the inputs actually exist."
    ),
    parameters={
        "type": "object",
        "properties": {"run_id": {"type": "string", "minLength": 1, "maxLength": 64}},
        "required": ["run_id"],
        "additionalProperties": False,
    },
    tier=ToolTier.READ,
    max_result_bytes=16_384,
)

GET_STATISTICAL_RESULTS_TOOL = LlmTool(
    name="get_statistical_results",
    description="List statistical results already registered for a run.",
    parameters={
        "type": "object",
        "properties": {"run_id": {"type": "string", "minLength": 1, "maxLength": 64}},
        "required": ["run_id"],
        "additionalProperties": False,
    },
    tier=ToolTier.READ,
    max_result_bytes=16_384,
)

DESCRIBE_CORRECTIONS_TOOL = LlmTool(
    name="describe_statistical_capabilities",
    description=(
        "Describe the multiple-comparison corrections this installation actually "
        "supports, with their required parameters. Never guess a correction method "
        "or threshold without checking here first."
    ),
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    tier=ToolTier.READ,
)

LIST_SKILLS_TOOL = LlmTool(
    name="list_skills",
    description="List the reviewed rs-fMRI Skills available in this installation.",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    tier=ToolTier.READ,
)


def build_work_tool_bindings(
    *,
    get_run: GetRunCall,
    list_artifacts: ListArtifactsCall | None = None,
    list_results: ListResultsCall | None = None,
    correction_capabilities: CorrectionCapabilitiesCall | None = None,
    list_skills: ListSkillsCall | None = None,
    project_id: str | None = None,
    allowed_run_ids: frozenset[str] | None = None,
) -> list[ToolBinding]:
    """Bind the Work read catalog to injected application-service callables."""

    def scoped_run(arguments: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]] | None:
        run_id = str(arguments["run_id"])
        # A missing scope is retained for direct library callers and migration
        # compatibility.  Request-created dispatchers always provide both.
        if allowed_run_ids is not None and run_id not in allowed_run_ids:
            return None
        try:
            view = dict(get_run(run_id))
        except NotFoundError:
            return None
        if project_id is not None and str(view.get("project_id", "")) != project_id:
            return None
        return run_id, view

    def get_run_status(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        scoped = scoped_run(arguments)
        if scoped is None:
            return {
                "run_id": str(arguments["run_id"]),
                "status": "not_available_in_scope",
            }
        _, view = scoped
        # Path-free, aggregate-only projection.
        return {
            "run_id": view.get("run_id"),
            "state": view.get("state"),
            "stage": view.get("stage"),
            "attempt": view.get("attempt"),
            "qc_status": view.get("qc_status"),
            "cancel_requested": view.get("cancel_requested"),
        }

    def list_run_artifacts(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        scoped = scoped_run(arguments)
        if scoped is None:
            return {
                "run_id": str(arguments["run_id"]),
                "status": "not_available_in_scope",
                "total": 0,
                "artifact_types": [],
            }
        run_id, _ = scoped
        if list_artifacts is None:
            return {"total": 0, "artifacts": []}
        items = list(list_artifacts(run_id))
        counts: dict[str, int] = {}
        for item in items:
            artifact_type = str(item.get("artifact_type", ""))
            counts[artifact_type] = counts.get(artifact_type, 0) + 1
        return {
            "total": len(items),
            "artifact_types": [
                {"artifact_type": name, "count": count}
                for name, count in sorted(counts.items())[:MAX_ARTIFACTS_REPORTED]
            ],
        }

    def get_statistical_results(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        scoped = scoped_run(arguments)
        if scoped is None:
            return {
                "run_id": str(arguments["run_id"]),
                "status": "not_available_in_scope",
                "total": 0,
                "results": [],
            }
        run_id, _ = scoped
        if list_results is None:
            return {"total": 0, "results": []}
        items = list(list_results(run_id))
        return {
            "total": len(items),
            "results": [
                {
                    "result_id": str(item.get("result_id", "")),
                    "test": str(item.get("test", "")),
                    "correction": str(item.get("correction_method", "") or ""),
                    "created_at": str(item.get("created_at", "")),
                }
                for item in items[:MAX_ARTIFACTS_REPORTED]
            ],
        }

    def describe_corrections(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        del arguments
        if correction_capabilities is None:
            return {"total": 0, "corrections": []}
        items = list(correction_capabilities())
        return {
            "total": len(items),
            "corrections": [
                {
                    "method": str(item.get("method", "")),
                    "label": str(item.get("label", "")),
                    "requires": [str(value) for value in item.get("required_parameters", ())],
                }
                for item in items[:MAX_CORRECTIONS_REPORTED]
            ],
        }

    def list_available_skills(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        del arguments
        if list_skills is None:
            return {"total": 0, "skills": []}
        items = list(list_skills())
        return {
            "total": len(items),
            "skills": [
                {
                    "skill_id": str(item.get("skill_id", "")),
                    "title": str(item.get("title", "")),
                    "status": str(item.get("status", "")),
                }
                for item in items[:MAX_SKILLS_REPORTED]
            ],
        }

    return [
        ToolBinding(tool=GET_RUN_STATUS_TOOL, handler=get_run_status),
        ToolBinding(tool=LIST_RUN_ARTIFACTS_TOOL, handler=list_run_artifacts),
        ToolBinding(tool=GET_STATISTICAL_RESULTS_TOOL, handler=get_statistical_results),
        ToolBinding(tool=DESCRIBE_CORRECTIONS_TOOL, handler=describe_corrections),
        ToolBinding(tool=LIST_SKILLS_TOOL, handler=list_available_skills),
    ]


def build_work_dispatcher(
    *,
    get_run: GetRunCall,
    policy: OutboundContextPolicy,
    list_artifacts: ListArtifactsCall | None = None,
    list_results: ListResultsCall | None = None,
    correction_capabilities: CorrectionCapabilitiesCall | None = None,
    list_skills: ListSkillsCall | None = None,
    audit: AuditSink | None = None,
    project_id: str | None = None,
    allowed_run_ids: frozenset[str] | None = None,
) -> ToolDispatcher:
    return ToolDispatcher(
        build_work_tool_bindings(
            get_run=get_run,
            list_artifacts=list_artifacts,
            list_results=list_results,
            correction_capabilities=correction_capabilities,
            list_skills=list_skills,
            project_id=project_id,
            allowed_run_ids=allowed_run_ids,
        ),
        policy=policy,
        audit=audit,
    )


class WorkToolLoopResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    final_text: str = ""
    stop_reason: str
    steps: int = Field(default=0, ge=0)
    tool_names: tuple[str, ...] = ()
    outcomes: tuple[ToolCallOutcome, ...] = ()
    usage: dict[str, int] = Field(default_factory=dict)
    context_hash: str = ""
    redaction_count: int = Field(default=0, ge=0)
    model: str | None = None
    profile_id: str | None = None
    attempted_profile_ids: tuple[str, ...] = ()


BuildMessages = Callable[..., ChatMessages]
CallTurn = Callable[..., Awaitable[ToolTurnResult]]
DispatcherFactory = Callable[[AgentExecutionContext], ToolInvoker]
CapabilityProbe = Callable[[str | None], Awaitable[bool]]


class WorkToolRunner:
    """Drive the bounded loop for Work; the final text is the Work JSON contract."""

    def __init__(
        self,
        *,
        build_messages: BuildMessages,
        call_turn: CallTurn,
        dispatcher_factory: DispatcherFactory,
        probe_capability: CapabilityProbe,
        budget: AgentLoopBudget | None = None,
    ) -> None:
        self._build_messages = build_messages
        self._call_turn = call_turn
        self._dispatcher_factory = dispatcher_factory
        self._probe_capability = probe_capability
        self._budget = budget or AgentLoopBudget()

    async def available(self, preferred_profile_id: str | None) -> bool:
        try:
            return await self._probe_capability(preferred_profile_id)
        except Exception:
            logger.warning("Work tool capability probe failed", exc_info=True)
            return False

    async def run(
        self,
        *,
        question: str,
        recent_messages: Sequence[Mapping[str, Any]],
        pinned_context: Sequence[Mapping[str, Any]],
        conversation_summary: str | None,
        work_context: Mapping[str, Any],
        preferred_profile_id: str | None,
        model: str | None,
        execution_context: AgentExecutionContext | None = None,
        context_window_tokens: int = 16_384,
        max_output_tokens: int = 2_048,
    ) -> WorkToolLoopResult:
        rendered = self._build_messages(
            question=question,
            evidence=[],
            recent_messages=[dict(item) for item in recent_messages],
            pinned_context=[dict(item) for item in pinned_context],
            conversation_summary=conversation_summary,
            work_context=dict(work_context),
            allow_web_search=False,
            work_mode=True,
        )
        dispatcher = self._dispatcher_factory(execution_context or AgentExecutionContext())
        tools: tuple[LlmTool, ...] = dispatcher.catalog

        messages: list[dict[str, Any]] = [dict(message) for message in rendered.messages]
        if messages and messages[0].get("role") == "system":
            system_content = str(messages[0].get("content") or "")
            messages[0] = {
                "role": "system",
                "content": f"{system_content}\n{UNTRUSTED_TOOL_RESULT_INSTRUCTION}",
            }

        captured: dict[str, Any] = {}

        async def call_model(
            transcript: list[dict[str, Any]], *, remaining_seconds: float | None = None
        ) -> ProviderResponse:
            turn = await self._call_turn(
                messages=transcript,
                tools=tools,
                preferred_profile_id=preferred_profile_id,
                model=model,
                remaining_seconds=remaining_seconds,
            )
            captured["profile_id"] = turn.selected_profile_id
            captured["attempted"] = turn.attempted_profile_ids
            return turn.response

        budget = replace(
            self._budget,
            context_window_tokens=context_window_tokens,
            max_output_tokens=max_output_tokens,
        )
        result = await AgentLoop(dispatcher, budget=budget).run(
            messages=messages,
            call_model=call_model,
        )
        return WorkToolLoopResult(
            final_text=result.final_text,
            stop_reason=result.stop_reason.value,
            steps=result.steps,
            tool_names=tuple(outcome.name for outcome in result.outcomes),
            outcomes=result.outcomes,
            usage=result.usage,
            context_hash=rendered.context_hash,
            redaction_count=rendered.redaction_count,
            model=result.provider_model,
            profile_id=captured.get("profile_id"),
            attempted_profile_ids=tuple(captured.get("attempted", ())),
        )
