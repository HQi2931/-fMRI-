"""Model interpretation produces drafts; only server-checked commands authorize actions."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from neuroagent.agent.gateway import ChatGatewayResult
from neuroagent.agent.loop import AgentLoopBudget
from neuroagent.agent.providers import ProviderTimeoutError
from neuroagent.agent.streaming import current_answer_stream
from neuroagent.application.contracts import (
    ConversationAction,
    ConversationTurnCreate,
    ConversationView,
    ExternalAnalysisImportView,
    ExternalDemographicsRevisionView,
    QcExecutionMode,
    WorkspaceCitation,
)
from neuroagent.application.conversation_work import ConversationWorkCoordinator, WorkActionResult
from neuroagent.application.errors import ApplicationError
from neuroagent.application.statistical_capabilities import model_visible_capabilities
from neuroagent.application.statistics_draft import (
    DesignIssue,
    StatisticalDesignProposal,
    StatisticsDraft,
    merge_statistics_draft,
    render_statistics_draft_review,
    statistics_draft_hash,
    statistics_draft_issues,
)
from neuroagent.application.work_actions import (
    SERVER_ONLY_ACTIONS,
    dispatch_target,
    is_model_selectable,
)
from neuroagent.application.work_agents import (
    analysis_dependency_for_request,
    handoff_identity,
    latest_agent_payload,
    latest_analysis_dependency,
    latest_handoff_for_agent,
    recent_messages_for_agent,
    work_agent_role_for_turn,
    work_agent_routing_question,
)
from neuroagent.application.work_draft import (
    WorkDraft,
    draft_hash,
    draft_schema,
    draft_snapshot,
    execution_summary,
    invalidate_input_evidence,
    merge_work_draft,
    plan_preview,
    planning_decisions,
    render_draft_review,
    review_issues,
)
from neuroagent.application.work_handoffs import (
    WorkAnalysisHandoff,
    WorkPreprocessingHandoff,
    handoff_reference_summary,
    require_same_handoff,
    require_same_preprocessing_handoff,
)
from neuroagent.application.work_presentation import (
    constrained_patch,
    leaf_values,
    merge_observed_facts,
    observed_facts,
    pending_review_questions,
    set_value,
)
from neuroagent.application.work_tasks import preprocessing_source_run
from neuroagent.application.work_tools import AgentExecutionContext, WorkToolRunner
from neuroagent.context.engine import RegexTokenCounter
from neuroagent.context.interfaces import ContextPacket
from neuroagent.multi_agent.registry import (
    WorkAgentRole,
    resolve_work_agent,
    work_agent_allows_action,
)
from neuroagent.observability.work_timings import measure_work_stage

logger = logging.getLogger(__name__)


class WorkQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=400)
    parameter_paths: tuple[str, ...] = Field(default=(), max_length=8)


class WorkInterpretation(BaseModel):
    """The model's interpretation of one Work turn.

    ``action`` is validated against the model-selectable tier set rather than a
    hand-written literal, so a server-only action (starting a run, approving or
    skipping QC) can never be selected by a model even if the prompt drifts.
    """

    model_config = ConfigDict(extra="forbid")
    answer: str = Field(min_length=1, max_length=12000)
    action: str = Field(default="conversation", max_length=64)
    draft_patch: dict[str, Any] = Field(default_factory=dict)
    analysis_patch: dict[str, Any] = Field(default_factory=dict)
    handoff_reason: str | None = Field(default=None, max_length=1000)
    required_preprocessing_outputs: tuple[str, ...] = Field(default=(), max_length=12)
    workspace_citations: tuple[WorkspaceCitation, ...] = ()
    workspace_edits: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    recommended_input_stage: str | None = Field(default=None, max_length=64)
    stage_intent: str | None = Field(default=None, max_length=1000)
    stage_reasoning: str | None = Field(default=None, max_length=4000)
    stage_questions: tuple[str, ...] = ()
    questions: tuple[WorkQuestion | str, ...] = Field(default=(), max_length=5)
    recommendation_reason: str | None = Field(default=None, max_length=1000)

    @field_validator("action")
    @classmethod
    def action_must_be_model_selectable(cls, value: str) -> str:
        if not is_model_selectable(value):
            raise ValueError(f"action {value!r} is not available to a model")
        return value


def latest_payload(conversation: ConversationView, key: str) -> dict[str, Any] | None:
    for message in reversed(conversation.messages):
        value = message.payload.get(key)
        if isinstance(value, dict):
            return value
    return None


def _snapshot_mapping(snapshot: dict[str, Any] | None, key: str) -> dict[str, Any] | None:
    value = (snapshot or {}).get(key)
    return value if isinstance(value, dict) else None


def _snapshot_sources(snapshot: dict[str, Any] | None) -> dict[str, str]:
    value = _snapshot_mapping(snapshot, "sources") or {}
    return {str(key): source for key, source in value.items() if isinstance(source, str)}


def _snapshot_string_list(snapshot: dict[str, Any] | None, key: str) -> list[str]:
    value = (snapshot or {}).get(key)
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _snapshot_int(snapshot: dict[str, Any] | None, key: str, default: int = 0) -> int:
    value = (snapshot or {}).get(key)
    return value if isinstance(value, int) else default


def _draft_sources(saved: dict[str, Any] | None, patch: dict[str, Any]) -> dict[str, str]:
    sources = dict(saved.get("sources", {})) if saved else {}
    old = saved.get("draft", {}) if saved else {}

    def mark(value: Any, prior: Any, prefix: str = "") -> None:
        if isinstance(value, dict):
            for name, child in value.items():
                mark(
                    child,
                    prior.get(name) if isinstance(prior, dict) else None,
                    f"{prefix}.{name}" if prefix else name,
                )
        elif value != prior or prefix not in sources:
            sources[prefix] = "suggested"

    mark(patch, old)
    return sources


def execution_command(plan_hash: str, qc: str, confirmation_id: str | None = None) -> str:
    suffix = f" {confirmation_id}" if confirmation_id else ""
    return f"批准并启动 {plan_hash[:12]} {qc}{suffix}"


def _work_loop_metadata(looped: Any) -> dict[str, Any]:
    """Persist loop outcomes even when the final answer requires fallback."""

    workspace_redactions = 0
    workspace_omitted_sensitive = 0
    for outcome in looped.outcomes:
        if outcome.name not in {"read_workspace_content", "search_workspace_content"}:
            continue
        result = outcome.result or {}
        workspace_redactions += int(result.get("redactions", 0) or 0)
        workspace_omitted_sensitive += int(result.get("omitted_sensitive", 0) or 0)
    return {
        "enabled": True,
        "stop_reason": looped.stop_reason,
        "steps": looped.steps,
        "tool_names": list(looped.tool_names),
        "usage": looped.usage,
        "fallback_candidate": not bool(looped.final_text.strip()),
        "fallback_used": False,
        "workspace_redactions": workspace_redactions,
        "workspace_omitted_sensitive": workspace_omitted_sensitive,
        "calls": [
            {
                "tool_name": outcome.name,
                "status": outcome.status.value,
                "risk_tier": outcome.tier.value if outcome.tier is not None else None,
                "step_index": index,
                "provider_call_id": outcome.call_id,
                "duration_ms": outcome.duration_ms,
                "input": outcome.arguments,
                "output": outcome.result,
                "error": outcome.error_code or outcome.message,
            }
            for index, outcome in enumerate(looped.outcomes, start=1)
        ],
    }


def _valid_workspace_citation_paths(
    citations: tuple[WorkspaceCitation, ...], loop_metadata: dict[str, Any] | None
) -> bool:
    """Accept citations only for successful reads in this turn.

    Search/list results help the model locate a file but are not evidence by
    themselves.  The server therefore binds every citation to a read tool
    result persisted in the bounded loop metadata.
    """

    if not loop_metadata:
        return not citations
    evidence: dict[str, list[dict[str, Any]]] = {}
    read_seen = False
    for call in loop_metadata.get("calls", []):
        if call.get("tool_name") != "read_workspace_content" or call.get("status") != "succeeded":
            continue
        read_seen = True
        output = call.get("output") or {}
        path = output.get("path")
        if not isinstance(path, str):
            continue
        locators = output.get("locators") or []
        evidence.setdefault(path, []).extend(
            locator for locator in locators if isinstance(locator, dict)
        )
    if read_seen and not citations:
        return False
    for citation in citations:
        if citation.path not in evidence:
            return False
        if citation.locator and not any(
            _locator_matches(citation.locator, locator) for locator in evidence[citation.path]
        ):
            return False
    return True


def _locator_matches(requested: str, locator: dict[str, Any]) -> bool:
    normalized = requested.casefold().replace(" ", "")
    flattened = "".join(f"{key}{value}" for key, value in locator.items())
    if normalized in flattened.casefold().replace(" ", ""):
        return True
    for key, value in locator.items():
        if normalized in f"{key}{value}".casefold().replace(" ", ""):
            return True
        if normalized == str(value).casefold().replace(" ", ""):
            return True
        if isinstance(value, dict) and _locator_matches(requested, value):
            return True
    return requested == str(locator)


# The user echoes this exact, server-issued command; a model cannot forge it
# because authorization is never taken from model output.  The pattern and
# ``execution_command`` must stay in step.
EXECUTION_COMMAND_PATTERN = r"批准并启动 ([a-f0-9]{12}) (做头动QC|跳过QC) ([a-f0-9-]{36})"
STAGE_CONFIRMATION_PATTERN = r"确认起始阶段 ([A-Za-z0-9_]+) ([a-f0-9]{12})"


def _work_failure_details(
    exc: Exception,
    metadata: dict[str, Any],
    stage: str,
) -> tuple[dict[str, Any], str]:
    """Record fixed categories and schema locations, never input or exception prose."""
    finish = metadata.get("finish_reason")
    if finish not in {None, "stop", "length", "tool_calls", "content_filter", "function_call"}:
        finish = "other"
    diagnostic: dict[str, Any] = {"stage": stage, "finish_reason": finish}
    usage = metadata.get("usage")
    if isinstance(usage, dict):
        diagnostic["usage"] = {
            key: value
            for key, value in usage.items()
            if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
            and isinstance(value, int)
            and value >= 0
        }
    if isinstance(exc, ValidationError):
        diagnostic["code"] = (
            "work_response_invalid" if stage == "response_validation" else "work_draft_invalid"
        )
        diagnostic["issues"] = [
            {
                "field": str(issue["loc"][0])
                if issue["loc"] and issue["loc"][0] in WorkInterpretation.model_fields
                else "root_or_unknown_field",
                "type": issue["type"],
            }
            for issue in exc.errors(include_input=False, include_context=False)[:20]
        ]
        reason = "原因：模型返回的 Work JSON 未通过结构校验，请重试。"
    elif isinstance(exc, ApplicationError):
        diagnostic["code"] = (
            exc.code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", exc.code) else "application_error"
        )
        diagnostic["status"] = exc.status_code
        reason = "原因：模型请求或服务端校验失败（" + diagnostic["code"] + "），请重试。"
        if exc.code == "work_model_timeout":
            diagnostic["provider_timeout"] = True
            reason = "原因：模型请求超时，请稍后重试或调整模型超时设置。"
    else:
        diagnostic["code"] = "work_draft_invalid"
        reason = "原因：模型提出的动作或参数草稿未通过校验，请重新说明需求。"
    if finish == "length" and stage == "response_validation":
        diagnostic["code"] = "work_response_truncated"
        reason = "原因：模型输出达到 token 上限，返回内容不完整。请在模型设置中提高输出上限后重试。"
    cause: BaseException | None = exc
    for _ in range(6):
        if cause is None:
            break
        # Match only adapter-defined status messages; never store arbitrary prose.
        from neuroagent.agent.providers import ProviderError

        if isinstance(cause, ProviderError):
            match = re.fullmatch(r"provider rejected request \(([0-9]{3})\)", str(cause))
            if match:
                diagnostic["provider_status"] = int(match[1])
                reason = (
                    "原因：模型服务拒绝请求（HTTP "
                    + match[1]
                    + "），请检查连接配置和接口兼容性后重试。"
                )
            if getattr(cause, "timeout", False):
                diagnostic["provider_timeout"] = True
                reason = "原因：模型请求超时，请稍后重试或调整模型超时设置。"
        cause = cause.__cause__
    return diagnostic, reason


class WorkDialogue:
    def __init__(
        self,
        coordinator: ConversationWorkCoordinator,
        generate: Callable[..., Awaitable[ChatGatewayResult]],
        tool_runner: WorkToolRunner | None = None,
        create_workspace_proposal: Callable[..., dict[str, Any]] | None = None,
        confirm_workspace_stage: Callable[..., Any] | None = None,
        prepare_statistical_proposal: Callable[
            ..., tuple[StatisticalDesignProposal | None, tuple[DesignIssue, ...]]
        ]
        | None = None,
        get_external_import: Callable[[str], ExternalAnalysisImportView] | None = None,
        get_external_demographics: Callable[[str], ExternalDemographicsRevisionView | None]
        | None = None,
        prepare_analysis_handoff: Callable[..., WorkAnalysisHandoff] | None = None,
        prepare_preprocessing_handoff: Callable[..., WorkPreprocessingHandoff] | None = None,
        get_result_facts: Callable[[str, bool], list[dict[str, Any]]] | None = None,
        model_budget: AgentLoopBudget | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.generate = generate
        # Optional: without it Work keeps the single-shot interpretation path.
        self.tool_runner = tool_runner
        self.create_workspace_proposal = create_workspace_proposal
        self.confirm_workspace_stage = confirm_workspace_stage
        self.prepare_statistical_proposal = prepare_statistical_proposal
        self.get_external_import = get_external_import
        self.get_external_demographics = get_external_demographics
        self.prepare_analysis_handoff = prepare_analysis_handoff
        self.prepare_preprocessing_handoff = prepare_preprocessing_handoff
        self.get_result_facts = get_result_facts
        self.model_budget = model_budget or AgentLoopBudget()

    def input_recovery_confirmation(
        self, workspace_path: str | None, candidates: list[dict[str, Any]]
    ) -> dict[str, Any]:
        token = (
            self.coordinator.stage_confirmation_token(workspace_path, candidates)
            if workspace_path and candidates
            else None
        )
        return {
            "title": "重新确认输入阶段",
            "summary": "需要重新核实输入。请确认本次使用的阶段，系统将重新冻结输入；此操作不启动计算。",
            "options": [
                {
                    "label": f"使用 {item['stage']}",
                    "description": "重新冻结当前输入，保留参数选择。",
                    "command": f"确认起始阶段 {item['stage']} {token[:12]}",
                }
                for item in candidates
                if token and isinstance(item, dict) and item.get("selectable") and item.get("stage")
            ],
        }

    def run_confirmation_request(
        self,
        *,
        conversation_id: str,
        project_id: str,
        plan_revision_id: str,
        plan_hash: str,
        qc_mode: QcExecutionMode | None,
        summary: str,
    ) -> dict[str, Any]:
        """Create final run choices without making QC a plan-compilation input."""
        if qc_mode is not None:
            choices = [
                (
                    qc_mode,
                    "做头动QC" if qc_mode is QcExecutionMode.REQUIRE else "跳过QC",
                    "确认并启动计算",
                    "仅授权本次方案与所选 QC 方式。",
                )
            ]
        else:
            choices = [
                (
                    QcExecutionMode.REQUIRE,
                    "做头动QC",
                    "启动并做头动 QC",
                    "计算后生成头动 QC 草稿，供人工审查。",
                ),
                (
                    QcExecutionMode.SKIP,
                    "跳过QC",
                    "启动并跳过 QC",
                    "记录跳过决定，计算后不进行本次头动 QC。",
                ),
            ]
        options = []
        for mode, command_choice, label, description in choices:
            confirmation_id = self.coordinator.create_confirmation_challenge(
                conversation_id=conversation_id,
                project_id=project_id,
                plan_revision_id=plan_revision_id,
                plan_hash=plan_hash,
                qc_mode=mode,
            )
            options.append(
                {
                    "label": label,
                    "description": description,
                    "command": execution_command(plan_hash, command_choice, confirmation_id),
                }
            )
        return {
            "title": "确认并启动计算",
            "summary": summary,
            "options": options,
        }

    async def respond(
        self,
        conversation: ConversationView,
        request: ConversationTurnCreate,
        packet: ContextPacket,
        *,
        workspace_path: str | None,
        project_id: str | None,
        preferred_profile_id: str | None,
        workspace_content_consent: bool = False,
        idempotency_key: str,
        has_profiles: bool,
    ) -> WorkActionResult:
        run_context = packet.work_context.get("run")
        qc_context = packet.work_context.get("qc")
        qc_status = (
            str(run_context.get("qc_status", "")).casefold()
            if isinstance(run_context, dict)
            else ""
        )
        qc_review_state = (
            str(qc_context.get("state", "")).casefold() if isinstance(qc_context, dict) else ""
        )
        analysis_ready = bool(
            isinstance(run_context, dict)
            and run_context.get("state") == "succeeded"
            and (
                qc_status in {"approved", "skipped", "qc_skipped"} or qc_review_state == "approved"
            )
        )
        requested_role = (
            (request.agent_role or WorkAgentRole.ANALYSIS)
            if request.external_analysis_import_id
            else request.agent_role
        )
        routing_question = work_agent_routing_question(
            request.content, requested=requested_role, conversation=conversation
        )
        role = (
            conversation.foreground_agent_role
            if routing_question
            else work_agent_role_for_turn(
                request.content,
                requested=requested_role,
                conversation=conversation,
                analysis_ready=analysis_ready,
            )
        )
        agent_spec = resolve_work_agent(role)
        request = request.model_copy(update={"agent_role": role})
        received_handoff: WorkPreprocessingHandoff | None = None

        def reply(text: str, payload: dict[str, Any] | None = None) -> WorkActionResult:
            body = {
                "agent_role": role.value,
                "agent_name": agent_spec.display_name,
                **(payload or {}),
            }
            if (
                role is WorkAgentRole.PREPROCESSING
                and received_handoff is not None
                and "agent_handoff" not in body
            ):
                body["agent_handoff"] = received_handoff.model_dump(mode="json")
            dependency = analysis_dependency_for_request(request.content, project_id)
            if dependency is None:
                dependency = latest_analysis_dependency(conversation)
            if dependency is not None:
                if role is WorkAgentRole.PREPROCESSING:
                    references: dict[str, Any] = {
                        "project_id": project_id,
                        "source_run_id": (
                            run_context.get("run_id") if isinstance(run_context, dict) else None
                        ),
                        "source_run_state": (
                            run_context.get("state") if isinstance(run_context, dict) else None
                        ),
                        "qc_status": qc_status or None,
                    }
                    plan_context = packet.work_context.get("plan")
                    if isinstance(plan_context, dict):
                        references["source_plan_revision_id"] = plan_context.get("plan_revision_id")
                        references["source_plan_hash"] = plan_context.get("plan_hash")
                    dependency["data_references"] = references
                    dependency["evidence_hash"] = handoff_identity(
                        {"task_id": dependency.get("task_id"), "data_references": references}
                    )
                    dependency["status"] = (
                        "analysis_ready" if analysis_ready else "waiting_for_preprocessing"
                    )
                    source_run_id = references.get("source_run_id")
                    if self.prepare_analysis_handoff and source_run_id and project_id:
                        proof = self.prepare_analysis_handoff(
                            str(source_run_id), project_id, verify_files=analysis_ready
                        )
                        body["agent_handoff"] = proof.model_dump(mode="json")
                        dependency["data_references"] = proof.data_references
                        dependency["evidence_hash"] = proof.evidence_hash
                        dependency["status"] = (
                            "analysis_ready"
                            if proof.status == "ready"
                            else "waiting_for_preprocessing"
                        )
                elif role is WorkAgentRole.ANALYSIS:
                    dependency["status"] = "analysis_started"
                body["analysis_dependency"] = dependency
            proposal = body.get("statistical_design_proposal")
            if (
                role is WorkAgentRole.ANALYSIS
                and isinstance(proposal, dict)
                and proposal.get("source_run_id")
                and project_id
                and self.prepare_analysis_handoff
            ):
                current = self.prepare_analysis_handoff(str(proposal["source_run_id"]), project_id)
                frozen_raw = latest_handoff_for_agent(conversation, role)
                try:
                    frozen_refs = frozen_raw.get("data_references") if frozen_raw else None
                    frozen = (
                        WorkAnalysisHandoff.model_validate(frozen_raw)
                        if frozen_raw
                        and isinstance(frozen_refs, dict)
                        and frozen_refs.get("source_run_id") == proposal["source_run_id"]
                        and frozen_raw.get("status") != "blocked"
                        else current
                    )
                    proof = require_same_handoff(frozen, current)
                    body["agent_handoff"] = proof.model_dump(mode="json")
                except (ApplicationError, ValidationError):
                    body.pop("statistical_design_proposal", None)
                    body["agent_handoff"] = current.model_copy(
                        update={"status": "stale" if current.status == "ready" else "blocked"}
                    ).model_dump(mode="json")
                    body["statistical_design_issues"] = [
                        "预处理交接尚未就绪或证据已变化，请重新核实来源及 QC。"
                    ]
                    if isinstance(body.get("analysis_dependency"), dict):
                        body["analysis_dependency"]["status"] = "blocked"
                    text = "统计草稿已保留；预处理交接尚未就绪或已失效，不能确认这份统计方案。"
            return WorkActionResult(
                text,
                payload=body,
                workspace_path=workspace_path,
                project_id=project_id,
                active_run_id=request.target_run_id or conversation.active_run_id,
            )

        if routing_question:
            return reply(
                routing_question,
                {"agent_routing": {"status": "needs_clarification", "agent_role": role.value}},
            )

        if role is WorkAgentRole.PREPROCESSING:
            pending = latest_handoff_for_agent(conversation, role, pending_only=True)
            if pending is not None:
                try:
                    frozen = WorkPreprocessingHandoff.model_validate(pending)
                    refs = frozen.data_references
                    if not project_id or self.prepare_preprocessing_handoff is None:
                        raise ApplicationError("work_handoff_unavailable", "交接证据服务不可用。")
                    current = self.prepare_preprocessing_handoff(
                        conversation_id=conversation.conversation_id,
                        project_id=project_id,
                        run_id=refs.get("source_run_id"),
                        external_import_id=refs.get("external_import_id"),
                        required_outputs=frozen.required_outputs,
                        reason=frozen.reason,
                    )
                    received_handoff = require_same_preprocessing_handoff(frozen, current)
                except (ApplicationError, ValidationError):
                    return reply(
                        "上游指标需求的来源证据已变化或尚未核实，未修改预处理方案。请回到分析 Agent 更新交接。",
                        {
                            "agent_handoff": {
                                **pending,
                                "status": "stale",
                                "blocking_items": ["work_handoff_stale"],
                            }
                        },
                    )

        if role is WorkAgentRole.ANALYSIS:
            return await self._respond_analysis(
                conversation,
                request,
                packet,
                workspace_path=workspace_path,
                project_id=project_id,
                preferred_profile_id=preferred_profile_id,
                workspace_content_consent=workspace_content_consent,
                idempotency_key=idempotency_key,
                has_profiles=has_profiles,
                reply=reply,
            )

        saved = latest_agent_payload(conversation, "work_draft", WorkAgentRole.PREPROCESSING)
        # A start confirmation is single use.  It is read before the workspace
        # match so that re-selecting a workspace cannot resurrect a used one.
        start_confirmation_used = bool(saved and saved.get("consumed"))
        if saved and saved.get("workspace_path") != workspace_path:
            saved = None
        draft = WorkDraft.model_validate(saved["draft"]) if saved else WorkDraft()
        profile = packet.work_context.get("workspace_data_profile")
        input_revision = profile.get("metadata_revision") if isinstance(profile, dict) else None

        def input_recovery_confirmation() -> dict[str, Any]:
            return self.input_recovery_confirmation(
                workspace_path, list(packet.work_context.get("workspace_stage_candidates", []))
            )

        invalidated = invalidate_input_evidence(saved, input_revision) if saved else None
        if invalidated:
            return reply(
                (
                    "输入文件已变化，旧读取结果和启动确认已失效。"
                    if invalidated["input_invalidation_reason"] == "changed"
                    else "历史读取结果尚未绑定当前输入，旧核实状态和启动确认已撤回。"
                )
                + "已保留你的参数选择，请重新读取采集信息，再审查新方案。",
                {
                    "work_draft": invalidated,
                    "draft_issues": invalidated["review_issues"],
                    "confirmation_request": input_recovery_confirmation(),
                    "plan_preview": plan_preview(draft),
                    "planning_decisions": planning_decisions(draft, invalidated["sources"]),
                },
            )
        text = request.content.strip()

        stage_confirmation = re.fullmatch(STAGE_CONFIRMATION_PATTERN, text)
        if stage_confirmation:
            if self.confirm_workspace_stage is None or not workspace_path:
                return reply("当前服务不能确认工作区起始阶段。")
            confirm_stage = self.confirm_workspace_stage
            dataset_payload = latest_payload(conversation, "dataset")
            candidates = list(packet.work_context.get("workspace_stage_candidates", []))
            stage = stage_confirmation[1]
            token = stage_confirmation[2]

            def commit_stage() -> WorkActionResult:
                expected_token = self.coordinator.stage_confirmation_token(
                    workspace_path, candidates
                )
                if token != expected_token[:12]:
                    return reply("工作区候选已变化，请重新审查并确认起始阶段。")
                registration_payload: dict[str, Any] = {}
                bound_project_id = project_id
                bound_dataset = dataset_payload
                if not bound_dataset or not bound_dataset.get("dataset_id"):
                    # The exact user command authorizes both registration and
                    # stage selection; model suggestions never reach this path.
                    registered = self.coordinator.register_workspace_for_stage_confirmation(
                        request,
                        workspace_path=workspace_path,
                        conversation_id=conversation.conversation_id,
                        idempotency_key=idempotency_key,
                    )
                    registration_payload.update(registered.payload)
                    bound_project_id = registered.project_id
                    bound_dataset = registration_payload.get("dataset")
                    if not bound_dataset:
                        return registered
                manifest = confirm_stage(
                    dataset_id=str(bound_dataset["dataset_id"]),
                    workspace_path=workspace_path,
                    selected_input_stage=stage,
                    token=expected_token,
                    idempotency_key=idempotency_key,
                )
                refreshed = {**saved, "input_manifest_stale": False} if saved else None
                if refreshed:
                    registration_payload["work_draft"] = refreshed
                if saved and not review_issues(draft, _snapshot_mapping(saved, "facts")):
                    compiled = self._compile(
                        conversation,
                        request,
                        draft,
                        workspace_path,
                        bound_project_id,
                        idempotency_key + ":review",
                        proposal=True,
                        dataset_id=str(bound_dataset["dataset_id"]),
                        snapshot=refreshed,
                    )
                    return replace(
                        compiled,
                        payload={
                            **registration_payload,
                            "manifest": manifest.model_dump(mode="json"),
                            "selected_input_stage": stage,
                            "stage_selection_required": False,
                            **compiled.payload,
                        },
                    )
                return replace(
                    reply(
                        f"已确认 DPABI 起始阶段 {stage}。现在可以继续整理预处理参数。",
                        {
                            **registration_payload,
                            "manifest": manifest.model_dump(mode="json"),
                            "selected_input_stage": stage,
                            "stage_selection_required": False,
                        },
                    ),
                    project_id=bound_project_id,
                )

            return replace(reply(""), commit=commit_stage)
        if text.startswith("确认方案参数"):
            if not saved or text != f"确认方案参数 {draft_hash(draft)[:12]}":
                return reply("草稿已变化或工作区不匹配，请先查看当前参数草稿并确认最新版本。")
            if saved.get("compiled"):
                return reply("该参数草稿已经编译，请审查方案后单独确认运行。")
            issues = review_issues(draft, _snapshot_mapping(saved, "facts"))
            if issues:
                return reply("参数尚未完整，暂不能编译：\n" + "\n".join(issues))
            return replace(
                reply(""),
                commit=lambda: self._compile(
                    conversation,
                    request,
                    draft,
                    workspace_path,
                    project_id,
                    idempotency_key,
                ),
            )

        plan_data = latest_payload(conversation, "plan_revision")
        # Authorization is an exact, server-issued command, never a model or UI boolean.
        start = re.fullmatch(EXECUTION_COMMAND_PATTERN, text)
        legacy_start = re.fullmatch(r"批准并启动 ([a-f0-9]{12}) (做头动QC|跳过QC)", text)
        if legacy_start:
            return reply(
                "这条启动指令缺少当前方案对应的一次性确认标识。"
                "请先让系统签发最新确认指令，再原样回复。"
            )
        if start:
            if saved and (saved.get("input_evidence_stale") or saved.get("input_manifest_stale")):
                return reply("输入文件已变化，旧启动确认已失效。请重新读取并审查方案。")
            if start_confirmation_used:
                run_id = (saved or {}).get("consumed_run_id")
                if run_id:
                    result = self.coordinator.execute(
                        request.model_copy(update={"action": ConversationAction.GET_PROGRESS}),
                        workspace_path=workspace_path,
                        project_id=project_id,
                        active_run_id=str(run_id),
                        idempotency_key=idempotency_key,
                        conversation_id=conversation.conversation_id,
                    )
                    return replace(result, payload={**result.payload, "work_draft": saved})
                return reply(
                    "该启动确认已经使用过，不会再次排队。"
                    "如需重新运行，请先重新确认并编译参数草稿，再使用新生成的启动指令。"
                )
            if saved and not saved.get("compiled"):
                return reply("参数草稿有尚未编译的变更，请先确认并编译最新参数。")
            if not plan_data or start[1] != str(plan_data.get("plan_hash", ""))[:12]:
                return reply("运行确认与当前方案不匹配，请先生成并审查当前方案。")
            if request.expected_plan_hash and request.expected_plan_hash != plan_data["plan_hash"]:
                return reply("页面中的方案版本已变化，请刷新并重新审查后确认。")
            authorized = request.model_copy(
                update={
                    "action": ConversationAction.START_PREPROCESSING,
                    "plan_revision_id": plan_data["plan_revision_id"],
                    "expected_plan_hash": plan_data["plan_hash"],
                    "real_execution_confirmed": True,
                    "confirmation_id": start[3],
                    "qc_mode": QcExecutionMode.REQUIRE
                    if start[2] == "做头动QC"
                    else QcExecutionMode.SKIP,
                }
            )

            def commit_authorized_run() -> WorkActionResult:
                result = self.coordinator.execute(
                    authorized,
                    workspace_path=workspace_path,
                    project_id=project_id,
                    active_run_id=conversation.active_run_id,
                    idempotency_key=idempotency_key,
                    conversation_id=conversation.conversation_id,
                )
                if result.active_run_id is None:
                    # Nothing was queued, so the confirmation stays unused.
                    return result
                # Record the consumption in this turn's own payload.  The payload
                # scan is newest-first, so the used marker wins over the earlier
                # compiled draft; ``compiled`` falls back to False so a fresh
                # review can produce a new command for a deliberate re-run.
                return replace(
                    result,
                    payload={
                        **result.payload,
                        "work_draft": {
                            **(saved or {}),
                            "draft": (saved or {}).get("draft") or {},
                            "hash": (saved or {}).get("hash"),
                            "workspace_path": (saved or {}).get("workspace_path") or workspace_path,
                            "compiled": True,
                            "consumed": True,
                            "consumed_run_id": result.active_run_id,
                            "field_states": {
                                path: "included"
                                for path in leaf_values((saved or {}).get("draft") or {})
                            },
                        },
                    },
                )

            return replace(reply(""), commit=commit_authorized_run)

        if not has_profiles:
            # Preserve typed/local workflows, but do not guess an action for questions or negations.
            if re.search(
                r"不要|别|不运行|不执行|不启动|取消|仅|只是|什么|为什么|如何|怎么|[?？]", text
            ):
                return reply(
                    "这条消息不会启动任务。自然语言方法问答和多轮参数整理需要先在设置中连接模型。"
                )
            if (
                packet.work_context.get("workspace_stage_selection_required")
                and request.action is ConversationAction.PREPARE_PREPROCESSING_PLAN
            ):
                candidates = [
                    str(item.get("stage", ""))
                    for item in packet.work_context.get("workspace_stage_candidates", [])
                    if isinstance(item, dict)
                ]
                return reply(
                    "当前未连接支持工具调用的模型，不能可靠识别预处理意图。"
                    "请先选择并明确确认一个 DPABI 起始阶段："
                    + ("、".join(candidates) if candidates else "无可核验候选")
                    + "。本轮未执行操作。",
                    {"stage_candidates": candidates, "stage_selection_required": True},
                )
            offline_action = self.coordinator.resolve_action(
                request,
                workspace_path,
                conversation.active_run_id,
            )
            # Second line of defence.  ``resolve_action`` already cannot return a
            # server-only action, but keeping the check here means a future
            # regression in the keyword path is refused rather than dispatched.
            if offline_action.value in SERVER_ONLY_ACTIONS:
                return reply(
                    "当前未连接模型，无法可靠理解这条操作指令。请先在设置中连接模型；本轮未执行操作。"
                )
            return replace(
                reply(""),
                commit=lambda: self.coordinator.execute(
                    request.model_copy(update={"real_execution_confirmed": False}),
                    workspace_path=workspace_path,
                    project_id=project_id,
                    active_run_id=conversation.active_run_id,
                    idempotency_key=idempotency_key,
                    conversation_id=conversation.conversation_id,
                ),
            )

        model_metadata: dict[str, Any] = {}
        loop_metadata: dict[str, Any] | None = None
        failure_stage = "context"
        try:
            model_context: dict[str, Any] = {
                "question": request.content,
                "recent_messages": recent_messages_for_agent(
                    conversation, WorkAgentRole.PREPROCESSING
                ),
                "pinned_context": [],
                "conversation_summary": None,
                "work_context": {
                    **packet.work_context,
                    "agent_role": WorkAgentRole.PREPROCESSING.value,
                    "parameter_draft": draft.model_dump(mode="json", exclude={"confirmed_hash"}),
                    "planning_decisions": planning_decisions(draft, _snapshot_sources(saved)),
                    "draft_schema": draft_schema(),
                    "input_evidence": {
                        "requires_fresh_reads": (saved or {}).get("pending_evidence_paths", []),
                        "requires_refreeze": bool((saved or {}).get("input_manifest_stale")),
                    },
                },
                "context_window_tokens": int(packet.metadata.get("context_window_tokens", 16_384)),
                "max_output_tokens": int(packet.metadata.get("max_output_tokens", 2_048)),
            }
            handoff = latest_handoff_for_agent(
                conversation, WorkAgentRole.PREPROCESSING, pending_only=True
            )
            if handoff is not None:
                model_context["work_context"]["agent_handoff"] = {
                    key: handoff.get(key)
                    for key in (
                        "handoff_id",
                        "from_agent",
                        "reason",
                        "required_outputs",
                        "data_references",
                        "evidence_hash",
                    )
                }
                handoff_refs = handoff.get("data_references")
                if isinstance(handoff_refs, dict):
                    model_context["work_context"]["agent_handoff"]["data_references"] = (
                        handoff_reference_summary(handoff_refs)
                    )
            counter = RegexTokenCounter()
            budget = int(packet.metadata.get("token_budget", 0))
            while counter.count(model_context) > budget and model_context["recent_messages"]:
                model_context["recent_messages"].pop(0)
            if counter.count(model_context) > budget:
                return reply(
                    "当前模型上下文窗口不足以容纳完整参数结构，请在设置中选择更大上下文的模型。草稿已保留。"
                )
            packet.metadata["estimated_tokens"] = counter.count(model_context)
            packet.metadata["selected"]["messages"] = len(model_context["recent_messages"])
            packet.metadata["work_draft_included"] = True

            # Preferred path: let the model read what it needs before answering.
            # The loop's final text is the same JSON contract as the single-shot
            # call, so parsing and authorization below are unchanged.
            failure_stage = "model_request"
            raw_content, metadata, loop_metadata = await self._interpret(
                model_context,
                preferred_profile_id=preferred_profile_id,
                model=request.model,
                execution_context=AgentExecutionContext(
                    conversation_id=conversation.conversation_id,
                    project_id=project_id,
                    allowed_run_ids=frozenset(
                        run_id
                        for run_id in (request.target_run_id, conversation.active_run_id)
                        if run_id
                    ),
                    selected_profile_id=preferred_profile_id,
                    context_window_tokens=int(packet.metadata.get("context_window_tokens", 16_384)),
                    max_output_tokens=int(packet.metadata.get("max_output_tokens", 2_048)),
                    workspace_path=workspace_path,
                    workspace_file_scope=(
                        request.workspace_file_scope.paths if request.workspace_file_scope else ()
                    ),
                    workspace_content_consent=workspace_content_consent,
                    workspace_stage_candidates=tuple(
                        item
                        for item in model_context["work_context"].get(
                            "workspace_stage_candidates", []
                        )
                        if isinstance(item, dict)
                    ),
                    work_agent_role=str(
                        model_context["work_context"].get("agent_role", "preprocessing")
                    ),
                ),
            )
            model_metadata = metadata
            if metadata.get("stop_reason") in {"provider_timeout", "budget_time"}:
                raise ApplicationError(
                    "work_model_timeout", "模型工具阶段超时。", status_code=503
                ) from ProviderTimeoutError("provider tool loop deadline exceeded")
            if metadata.get("stop_reason") in {"provider_error", "cancelled"}:
                raise ApplicationError(
                    "work_model_unavailable", "模型工具阶段失败；未重复请求。", status_code=503
                )
            if metadata.get("stop_reason") in {"budget_steps", "budget_tokens"}:
                raise ApplicationError(
                    "work_model_budget_exhausted", "本轮模型预算已用完，草稿和证据已保留。"
                )
            failure_stage = "response_validation"
            if metadata.get("finish_reason") == "length":
                raise ApplicationError("work_response_truncated", "模型输出不完整。")
            interpreted = WorkInterpretation.model_validate_json(raw_content)
            if not work_agent_allows_action(role, interpreted.action):
                return reply(
                    "所选 Agent 不负责本次操作，未修改任何草稿或执行任务。请切换到对应角色。",
                    {"rejected_action": interpreted.action},
                )
            if interpreted.action in {
                "prepare_statistical_analysis",
                "request_preprocessing_handoff",
            }:
                return reply(
                    "该操作属于数据分析 Agent，预处理 Agent 本轮未修改草稿。请切换角色后继续。",
                    {
                        "agent_handoff": {
                            "from_agent": role.value,
                            "to_agent": WorkAgentRole.ANALYSIS.value,
                            "status": "proposed",
                        }
                    },
                )
            failure_stage = "draft_validation"
            payload: dict[str, Any] = {"model": metadata}
            if loop_metadata is not None:
                payload["tool_loop"] = {**loop_metadata, "agent_role": role.value}
                payload["workspace_redactions"] = loop_metadata.get("workspace_redactions", 0)
                payload["workspace_omitted_sensitive"] = loop_metadata.get(
                    "workspace_omitted_sensitive", 0
                )
            stage_profile = model_context["work_context"].get("workspace_data_profile")
            selected_stage = (
                str(stage_profile.get("selected_input_stage"))
                if isinstance(stage_profile, dict) and stage_profile.get("selected_input_stage")
                else None
            )
            stage_recommendation_needs_confirmation = bool(
                interpreted.recommended_input_stage
                and (
                    bool(model_context["work_context"].get("workspace_stage_selection_required"))
                    or selected_stage is None
                    or selected_stage.casefold() != interpreted.recommended_input_stage.casefold()
                )
            )
            recommended_stage = interpreted.recommended_input_stage
            if stage_recommendation_needs_confirmation and recommended_stage is not None:
                candidates = list(
                    model_context["work_context"].get("workspace_stage_candidates", [])
                )
                candidate_names = {
                    str(item.get("stage", "")).casefold()
                    for item in candidates
                    if isinstance(item, dict)
                }
                if recommended_stage.casefold() not in candidate_names:
                    return reply(
                        "模型建议的 DPABI 起始阶段不在服务端本轮候选清单中，已拒绝该建议；请重新读取工作区阶段。",
                        payload,
                    )
                selected_candidate = next(
                    (
                        item
                        for item in candidates
                        if isinstance(item, dict)
                        and str(item.get("stage", "")).casefold() == recommended_stage.casefold()
                    ),
                    None,
                )
                if selected_candidate is None or not bool(
                    selected_candidate.get("selectable", False)
                ):
                    stage_issues = (
                        selected_candidate.get("issues")
                        if isinstance(selected_candidate, dict)
                        else None
                    ) or ["候选阶段当前不可执行"]
                    return reply(
                        "模型建议的 DPABI 起始阶段已被服务端判定为不可执行："
                        + "；".join(str(issue) for issue in stage_issues),
                        payload,
                    )
                if workspace_path and candidates:
                    token = self.coordinator.stage_confirmation_token(workspace_path, candidates)
                    payload["workspace_stage_recommendation"] = {
                        "stage": interpreted.recommended_input_stage,
                        "intent": interpreted.stage_intent,
                        "reasoning": interpreted.stage_reasoning,
                        "questions": list(interpreted.stage_questions),
                        "confirmation_command": (
                            f"确认起始阶段 {interpreted.recommended_input_stage} {token[:12]}"
                        ),
                    }
                    self.coordinator.record_workspace_stage_recommendation(
                        conversation_id=conversation.conversation_id,
                        project_id=project_id,
                        workspace_path=workspace_path,
                        stage=recommended_stage,
                        intent=interpreted.stage_intent,
                        reasoning=interpreted.stage_reasoning,
                        questions=list(interpreted.stage_questions),
                    )
                    stage_summary = (
                        f"建议从 {recommended_stage} 输入开始预处理。"
                        "请核对输入阶段，再点击页面的确认按钮。"
                        "确认后冻结当前输入并继续准备方案；启动计算仍需审查最终方案。"
                    )
                    stage_command = payload["workspace_stage_recommendation"][
                        "confirmation_command"
                    ]
                    if stage_command:
                        payload["confirmation_request"] = {
                            "title": "确认预处理起点",
                            "summary": stage_summary,
                            "options": [
                                {
                                    "label": f"使用 {interpreted.recommended_input_stage}",
                                    "description": "登记当前工作区并确认输入阶段，继续准备方案。",
                                    "command": stage_command,
                                }
                            ],
                        }
                    if interpreted.action != "prepare_preprocessing_plan":
                        # A stage proposal is an unapproved UI operation. The
                        # model's prose may expose commands or falsely claim a
                        # run has started; explain the server-issued action only.
                        return reply(stage_summary, payload)
            if (
                interpreted.action == "prepare_preprocessing_plan"
                and not interpreted.draft_patch
                and bool(model_context["work_context"].get("workspace_stage_selection_required"))
            ):
                candidates = [
                    str(item.get("stage", ""))
                    for item in model_context["work_context"].get("workspace_stage_candidates", [])
                    if isinstance(item, dict)
                ]
                return reply(
                    "请先明确 DPABI 起始阶段。当前候选："
                    + ("、".join(candidates) if candidates else "无可核验阶段")
                    + "。模型可以根据你的处理意图提出建议，但必须由你确认后才能生成方案。",
                    payload,
                )
            if not _valid_workspace_citation_paths(interpreted.workspace_citations, loop_metadata):
                return reply(
                    "文件引用无法与本轮成功读取结果对应，已拒绝该结论或修改提案。请先读取文件后重试。",
                    payload,
                )
            if interpreted.workspace_citations:
                payload["workspace_citations"] = [
                    item.model_dump(mode="json") for item in interpreted.workspace_citations
                ]
            if interpreted.draft_patch and interpreted.action != "prepare_preprocessing_plan":
                raise ValueError("draft patch requires planning intent")
            if interpreted.action == "propose_workspace_changes":
                if self.create_workspace_proposal is None:
                    return reply("当前服务未启用工作区修改提案。", payload)
                try:
                    proposal = self.create_workspace_proposal(
                        conversation_id=conversation.conversation_id,
                        workspace_path=workspace_path,
                        profile_id=preferred_profile_id,
                        model=request.model,
                        edits=interpreted.workspace_edits,
                        citations=[
                            item.model_dump(mode="json") for item in interpreted.workspace_citations
                        ],
                    )
                except ApplicationError as exc:
                    return reply(exc.message, payload)
                payload["workspace_change_proposal"] = proposal
                return reply(
                    interpreted.answer + "\n\n已生成待审查补丁，请在界面中查看 diff 并确认应用。",
                    payload,
                )
            if interpreted.action == "prepare_preprocessing_plan":
                observer = current_answer_stream()
                if observer is not None:
                    await observer.status("正在校验并整理方案…")
                patch, sources, blocked = constrained_patch(
                    draft.model_dump(mode="json"),
                    _snapshot_sources(saved),
                    interpreted.draft_patch,
                    request.content,
                )
                with measure_work_stage("fact_derivation"):
                    current_facts = observed_facts(loop_metadata)
                    facts = merge_observed_facts(
                        _snapshot_mapping(saved, "facts") or {}, current_facts
                    )
                pending_evidence = sorted(
                    set(_snapshot_string_list(saved, "pending_evidence_paths")) - set(current_facts)
                )
                for path, value in facts.items():
                    if sources.get(path) != "user" and not isinstance(value, dict):
                        set_value(patch, path, value)
                draft = merge_work_draft(draft, patch)
                merged_preprocessing = draft.preprocessing or {}
                normalization = merged_preprocessing.get("normalization") or {}
                saved_preprocessing = (
                    _snapshot_mapping(_snapshot_mapping(saved, "draft"), "preprocessing") or {}
                )
                saved_normalization = saved_preprocessing.get("normalization")
                if not isinstance(saved_normalization, dict):
                    saved_normalization = {}
                if (
                    "structural_artifact_id" in normalization
                    and "preprocessing.normalization.structural_artifact_id"
                    not in leaf_values(patch)
                    and "structural_artifact_id" not in saved_normalization
                ):
                    sources["preprocessing.normalization.structural_artifact_id"] = "system"
                for path, value in facts.items():
                    if (
                        leaf_values(draft.model_dump(mode="json")).get(path) == value
                        and sources.get(path) != "user"
                    ):
                        sources[path] = "system" if path.endswith("slice_order") else "data"
                with measure_work_stage("validation"):
                    issues = review_issues(draft, facts)
                if pending_evidence:
                    issues += ("输入文件已变化，部分采集信息尚未重新读取，请核实后再启动。",)
                review = render_draft_review(draft, sources)
                snapshot = draft_snapshot(
                    draft,
                    workspace_path=workspace_path,
                    sources=sources,
                    prior=saved,
                    facts=facts,
                )
                snapshot["input_revision"] = input_revision
                snapshot["pending_evidence_paths"] = pending_evidence
                snapshot["input_evidence_stale"] = bool(pending_evidence)
                snapshot["review_issues"] = list(issues)
                payload["work_draft"] = snapshot
                payload["draft_issues"] = list(issues)
                payload["plan_preview"] = plan_preview(draft)
                payload["planning_decisions"] = planning_decisions(draft, sources)
                if snapshot.get("input_manifest_stale"):
                    payload["confirmation_request"] = input_recovery_confirmation()
                questions = pending_review_questions(
                    [
                        question.model_dump() if isinstance(question, WorkQuestion) else question
                        for question in interpreted.questions
                    ],
                    draft.model_dump(mode="json"),
                    sources,
                    facts,
                )
                payload["questions"] = questions
                explanation = ""
                reason = interpreted.recommendation_reason
                if (
                    reason
                    and not blocked
                    and not re.search(
                        r"(?:preprocessing|alff_falff|reho)\.[A-Za-z_]|\b[a-f0-9]{32,64}\b|workspace:[\w-]+|\bt1_segment\b",
                        reason,
                    )
                ):
                    explanation = "\n\n方案说明（模型建议）：" + reason[:240]
                    payload["recommendation_reason"] = reason
                if questions:
                    explanation += (
                        "\n\n"
                        + ("需要你决定：" if issues else "审查时可调整：")
                        + "\n"
                        + "\n".join(f"- {question}" for question in questions)
                    )
                review += explanation
                if (
                    not issues
                    and not snapshot.get("input_manifest_stale")
                    and selected_stage
                    and not model_context["work_context"].get("workspace_stage_selection_required")
                ):

                    def prepare_review() -> WorkActionResult:
                        result = self._compile(
                            conversation,
                            request,
                            draft,
                            workspace_path,
                            project_id,
                            idempotency_key + ":review",
                            proposal=True,
                            snapshot=snapshot,
                        )
                        return replace(
                            result,
                            assistant_content=result.assistant_content + explanation,
                            payload={**payload, **result.payload},
                        )

                    return replace(reply(review, payload), commit=prepare_review)
                if not issues and "confirmation_request" not in payload:
                    payload["confirmation_request"] = {
                        "title": "确认方案参数",
                        "summary": review,
                        "options": [
                            {
                                "label": "确认并生成方案",
                                "description": "登记工作区并校验、编译当前参数，不启动计算。",
                                "command": f"确认方案参数 {draft_hash(draft)[:12]}",
                            }
                        ],
                    }
                if (
                    workspace_path
                    and request.selected_input_stage
                    and model_context["work_context"].get("workspace_stage_selection_required")
                ):
                    candidates = model_context["work_context"].get("workspace_stage_candidates", [])
                    selected = next(
                        (
                            item
                            for item in candidates
                            if isinstance(item, dict)
                            and item.get("stage") == request.selected_input_stage
                            and item.get("selectable") is True
                        ),
                        None,
                    )
                    if selected is not None:
                        token = self.coordinator.stage_confirmation_token(
                            workspace_path, candidates
                        )
                        payload["confirmation_request"] = {
                            "title": "确认预处理起点",
                            "summary": f"使用你在页面选择的 {request.selected_input_stage} 输入阶段。确认后冻结输入并校验当前方案，不启动计算。",
                            "options": [
                                {
                                    "label": f"使用 {request.selected_input_stage}",
                                    "description": "核对输入阶段，准备当前方案。",
                                    "command": f"确认起始阶段 {request.selected_input_stage} {token[:12]}",
                                }
                            ],
                        }
                if blocked:
                    payload["preserved_user_choices"] = blocked
                    review += "\n已保留你明确指定的参数；未采用与这些选择冲突的模型建议。"
                # The structured panel carries every value and problem. Avoid
                # repeating the model's potentially stale parameter narrative.
                return reply(review, payload)
            if interpreted.action == "propose_run_confirmation":
                if not plan_data or (saved and not saved.get("compiled")):
                    return reply("请先补全并确认参数草稿，生成可审查的方案后再启动。", payload)
                if not project_id:
                    return reply("当前对话尚未绑定项目，无法签发启动确认。", payload)
                plan_revision_id = str(plan_data["plan_revision_id"])
                plan_hash = str(plan_data["plan_hash"])
                require_id = self.coordinator.create_confirmation_challenge(
                    conversation_id=conversation.conversation_id,
                    project_id=project_id,
                    plan_revision_id=plan_revision_id,
                    plan_hash=plan_hash,
                    qc_mode=QcExecutionMode.REQUIRE,
                )
                skip_id = self.coordinator.create_confirmation_challenge(
                    conversation_id=conversation.conversation_id,
                    project_id=project_id,
                    plan_revision_id=plan_revision_id,
                    plan_hash=plan_hash,
                    qc_mode=QcExecutionMode.SKIP,
                )
                payload["confirmation_request"] = {
                    "title": "批准并启动计算",
                    "summary": (
                        "批准后将启动当前方案的计算。"
                        "请选择本次头动 QC 方式；此授权仅用于本次运行。"
                        + ("\n\n" + render_draft_review(draft) if saved else "")
                    ),
                    "options": [
                        {
                            "label": "启动并做头动 QC",
                            "description": "计算后生成头动 QC 草稿，供人工审查。",
                            "command": execution_command(plan_hash, "做头动QC", require_id),
                        },
                        {
                            "label": "启动并跳过 QC",
                            "description": "记录跳过决定，计算后不进行本次头动 QC。",
                            "command": execution_command(plan_hash, "跳过QC", skip_id),
                        },
                    ],
                }
                return reply(
                    "方案已准备好。请在方案区域审查本次参数和 QC 选择，然后确认启动计算。",
                    payload,
                )
            target = dispatch_target(interpreted.action)
            if target is not None:
                action_request = request.model_copy(
                    update={
                        "action": target,
                        "real_execution_confirmed": False,
                    }
                )
                result = self.coordinator.execute(
                    action_request,
                    workspace_path=workspace_path,
                    project_id=project_id,
                    active_run_id=conversation.active_run_id,
                    idempotency_key=idempotency_key,
                    conversation_id=conversation.conversation_id,
                )
                return replace(
                    result, payload={**result.payload, **payload}, workspace_path=workspace_path
                )
            return reply(interpreted.answer, payload)
        except (ApplicationError, ValidationError, ValueError) as exc:
            diagnostic, reason = _work_failure_details(exc, model_metadata, failure_stage)
            logger.warning("Work turn rejected: %s", json.dumps(diagnostic, sort_keys=True))
            return reply(
                "本轮模型服务不可用或返回的结构不符合要求，未更改参数草稿，也未执行任务。"
                + "\n\n"
                + reason
                + "\n原有对话和草稿仍保留。",
                {
                    "model_error": diagnostic,
                    **({"tool_loop": loop_metadata} if loop_metadata is not None else {}),
                },
            )

    async def _interpret(
        self,
        model_context: dict[str, Any],
        *,
        preferred_profile_id: str | None,
        model: str | None,
        execution_context: AgentExecutionContext | None = None,
    ) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
        """Produce the Work JSON contract, preferring the bounded tool loop.

        The loop is an enrichment, not a replacement: a tool-capable model may
        read extra state first, but if the loop produces nothing usable (budget,
        provider error, or a non-JSON answer) this falls back to the single-shot
        call, which is the path that guarantees JSON mode today.
        """

        started = time.monotonic()
        spent_steps = 0
        spent_tokens = 0
        loop_metadata: dict[str, Any] | None = None
        tools_available = False
        if self.tool_runner is not None:
            try:
                tools_available = await asyncio.wait_for(
                    self.tool_runner.available(preferred_profile_id),
                    timeout=self.model_budget.max_seconds,
                )
            except TimeoutError:
                return "", {"stop_reason": "budget_time"}, None
        if self.tool_runner is not None and tools_available:
            remaining_seconds = self.model_budget.max_seconds - (time.monotonic() - started)
            if remaining_seconds <= 0:
                return "", {"stop_reason": "budget_time"}, None
            looped = await self.tool_runner.run(
                question=str(model_context["question"]),
                recent_messages=model_context["recent_messages"],
                pinned_context=model_context["pinned_context"],
                conversation_summary=model_context["conversation_summary"],
                work_context=model_context["work_context"],
                preferred_profile_id=preferred_profile_id,
                model=model,
                execution_context=execution_context,
                context_window_tokens=int(model_context.get("context_window_tokens", 16_384)),
                max_output_tokens=int(model_context.get("max_output_tokens", 2_048)),
                request_budget=replace(self.model_budget, max_seconds=remaining_seconds),
            )
            loop_metadata = _work_loop_metadata(looped)
            spent_steps = looped.steps
            spent_tokens = (
                looped.usage.get("total_tokens", 0)
                or (looped.usage.get("prompt_tokens", 0) + looped.usage.get("completion_tokens", 0))
                or max(1, looped.steps)
                * (looped.estimated_tokens + RegexTokenCounter().count(looped.final_text))
            )
            if spent_tokens > self.model_budget.max_total_tokens:
                return "", {"stop_reason": "budget_tokens", "usage": looped.usage}, loop_metadata
            if looped.stop_reason in {
                "provider_timeout",
                "budget_time",
                "provider_error",
                "cancelled",
                "budget_steps",
                "budget_tokens",
            }:
                return "", {"stop_reason": looped.stop_reason, "usage": looped.usage}, loop_metadata
            if looped.finish_reason == "length":
                # Never spend another complete request repeating a known
                # completion-budget failure. Response validation records it.
                return (
                    looped.final_text,
                    {
                        "profile_id": looped.profile_id,
                        "model": looped.model,
                        "finish_reason": looped.finish_reason,
                        "usage": looped.usage,
                    },
                    loop_metadata,
                )
            if looped.final_text.strip():
                metadata = {
                    "profile_id": looped.profile_id,
                    "model": looped.model,
                    "context_hash": looped.context_hash,
                    "redaction_count": looped.redaction_count,
                    "finish_reason": looped.finish_reason,
                    "usage": looped.usage,
                }
                if self._parses(looped.final_text):
                    return looped.final_text, metadata, loop_metadata
                # The model answered but not in the required structure; keep the
                # tool evidence recorded and let the single-shot path retry.
                logger.warning("Work tool loop answer failed the JSON contract; retrying once")

        fallback_context = {
            key: value
            for key, value in model_context.items()
            if key not in {"context_window_tokens", "max_output_tokens"}
        }
        if loop_metadata is not None:
            fallback_context["work_context"] = {
                **fallback_context.get("work_context", {}),
                "successful_read_evidence": [
                    call.get("output")
                    for call in loop_metadata.get("calls", [])
                    if call.get("tool_name") == "read_workspace_content"
                    and call.get("status") == "succeeded"
                ][-5:],
            }
            fallback_context["question"] = str(fallback_context["question"]) + (
                "\n本轮只重试一次结构化回复：返回简洁的 answer、action 和增量 draft_patch，"
                "省略没有内容的可选字段。prepare_preprocessing_plan 只能作为最终 JSON 的 action 字段值，"
                "prepare_statistical_analysis 也只能作为最终 JSON 的 action 字段值，不是工具；"
                "只引用 successful_read_evidence 中逐字提供的 path 和 locator。"
            )
        remaining_seconds = self.model_budget.max_seconds - (time.monotonic() - started)
        reason = None
        if remaining_seconds <= 0:
            reason = "budget_time"
        elif spent_steps >= self.model_budget.max_steps:
            reason = "budget_steps"
        elif (
            spent_tokens
            + RegexTokenCounter().count(fallback_context)
            + int(model_context.get("max_output_tokens", 2048))
            > self.model_budget.max_total_tokens
        ):
            reason = "budget_tokens"
        if reason:
            return "", {"stop_reason": reason}, loop_metadata
        try:
            generated = await asyncio.wait_for(
                self.generate(
                    **fallback_context,
                    evidence=[],
                    preferred_profile_id=preferred_profile_id,
                    model=model,
                    allow_web_search=False,
                    work_mode=True,
                ),
                timeout=remaining_seconds,
            )
        except TimeoutError:
            return "", {"stop_reason": "provider_timeout"}, loop_metadata
        metadata = {
            "profile_id": generated.selected_profile_id,
            "model": generated.response.model,
            "context_hash": generated.context_hash,
            "redaction_count": generated.redaction_count,
            "finish_reason": generated.response.finish_reason,
            "usage": generated.response.usage,
        }
        fallback_tokens = (
            generated.response.usage.get("total_tokens", 0)
            or generated.response.usage.get("prompt_tokens", 0)
            + generated.response.usage.get("completion_tokens", 0)
            or RegexTokenCounter().count(fallback_context)
            + RegexTokenCounter().count(generated.response.content)
        )
        metadata["request_budget"] = {
            "model_steps": spent_steps + 1,
            "total_tokens": spent_tokens + fallback_tokens,
            "max_steps": self.model_budget.max_steps,
            "max_total_tokens": self.model_budget.max_total_tokens,
        }
        if loop_metadata is not None:
            loop_metadata = {
                **loop_metadata,
                "fallback_used": True,
                "fallback_reason": "tool_loop_unusable_or_incomplete",
                "request_model_steps": spent_steps + 1,
                "request_total_tokens": spent_tokens + fallback_tokens,
            }
        if spent_tokens + fallback_tokens > self.model_budget.max_total_tokens:
            metadata["stop_reason"] = "budget_tokens"
            return "", metadata, loop_metadata
        return generated.response.content, metadata, loop_metadata

    async def _respond_analysis(
        self,
        conversation: ConversationView,
        request: ConversationTurnCreate,
        packet: ContextPacket,
        *,
        workspace_path: str | None,
        project_id: str | None,
        preferred_profile_id: str | None,
        workspace_content_consent: bool,
        idempotency_key: str,
        has_profiles: bool,
        reply: Callable[[str, dict[str, Any] | None], WorkActionResult],
    ) -> WorkActionResult:
        saved = latest_agent_payload(conversation, "analysis_draft", WorkAgentRole.ANALYSIS)
        try:
            draft = (
                StatisticsDraft.model_validate(saved.get("draft", {}))
                if saved
                else StatisticsDraft()
            )
        except ValidationError:
            draft = StatisticsDraft()
            saved = None
        external_id = (
            request.external_analysis_import_id
            if "external_analysis_import_id" in request.model_fields_set or request.target_run_id
            else draft.external_import_id
        )
        if external_id != draft.external_import_id or (external_id and draft.source_run_id):
            draft = draft.model_copy(
                update={
                    "source_run_id": None,
                    "qc_review_revision_id": None,
                    "qc_review_hash": None,
                    "qc_skip_id": None,
                    "qc_skip_hash": None,
                    "input_manifest_hash": None,
                    "demographics_revision_id": None,
                    "demographics_hash": None,
                    "confirmed_hash": None,
                    "metric_artifact_type": None,
                }
            )
        external_summary: dict[str, Any] | None = None
        if external_id:
            if request.target_run_id or not project_id or self.get_external_import is None:
                return reply("请为本次分析选择一个明确的来源和项目，统计草稿已保留。", None)
            try:
                imported = self.get_external_import(external_id)
            except ApplicationError as exc:
                return reply(str(exc), None)
            if (
                imported.project_id != project_id
                or imported.status != "included"
                or imported.selection is None
                or imported.inspection.metric_kind is None
                or imported.inspection.scaling is None
            ):
                return reply(
                    "外部来源不属于当前项目，或尚未完成纳入及掩膜登记。统计草稿已保留。", None
                )
            metric_type = f"external.{imported.inspection.metric_kind.value}.{imported.inspection.scaling.value}"
            draft = draft.model_copy(
                update={
                    "external_import_id": external_id,
                    "external_import_hash": imported.inclusion_hash,
                    "source_run_id": None,
                    "metric_artifact_type": metric_type,
                }
            )
            external_summary = {
                "layout": imported.inspection.layout,
                "metric_artifact_type": metric_type,
                "grouping_column": "group" if imported.inspection.layout == "grouped" else None,
                "condition_column": "condition"
                if imported.inspection.layout == "paired_conditions"
                else None,
                "condition_order": list(imported.selection.group_order)
                if imported.inspection.layout == "paired_conditions"
                else [],
                "group_order": list(imported.selection.group_order),
                "subject_count": len({image.subject_id for image in imported.selection.images}),
                "unknown_processing_fields": list(imported.inspection.unknown_processing_fields),
            }
            demographics = (
                self.get_external_demographics(external_id)
                if self.get_external_demographics
                else None
            )
            external_summary["demographics"] = (
                {
                    "columns": demographics.columns,
                    "revision": demographics.revision,
                    "row_count": demographics.row_count,
                }
                if demographics
                else None
            )
        else:
            draft = draft.model_copy(
                update={"external_import_id": None, "external_import_hash": None}
            )
        if not has_profiles:
            offline_action = self.coordinator.resolve_action(
                request,
                workspace_path,
                request.target_run_id or conversation.active_run_id,
            )
            if offline_action is ConversationAction.GET_STATISTICAL_RESULTS:
                return replace(
                    reply("", None),
                    commit=lambda: self.coordinator.execute(
                        request.model_copy(update={"action": offline_action}),
                        workspace_path=workspace_path,
                        project_id=project_id,
                        active_run_id=request.target_run_id or conversation.active_run_id,
                        idempotency_key=idempotency_key,
                        conversation_id=conversation.conversation_id,
                    ),
                )
        if not has_profiles:
            return reply(
                "数据分析 Agent 需要连接模型才能理解分析需求；现有统计结果仍可在会话中查看。",
                {
                    "analysis_draft": {
                        "draft": draft.model_dump(mode="json"),
                        "issues": list(statistics_draft_issues(draft)),
                    }
                },
            )

        model_context: dict[str, Any] = {
            "question": request.content,
            "recent_messages": recent_messages_for_agent(conversation, WorkAgentRole.ANALYSIS),
            "pinned_context": [],
            "conversation_summary": None,
            "work_context": {
                **{
                    key: value
                    for key, value in packet.work_context.items()
                    if key
                    not in {
                        "parameter_draft",
                        "planning_decisions",
                        "draft_schema",
                        "input_evidence",
                        "workspace_stage_candidates",
                        "workspace_stage_selection_required",
                        "selected_input_stage",
                    }
                },
                "agent_role": WorkAgentRole.ANALYSIS.value,
                "analysis_draft": draft.model_dump(
                    mode="json",
                    exclude={
                        "source_run_id",
                        "external_import_id",
                        "external_import_hash",
                        "qc_review_revision_id",
                        "qc_review_hash",
                        "qc_skip_id",
                        "qc_skip_hash",
                        "input_manifest_hash",
                        "demographics_revision_id",
                        "demographics_hash",
                        "confirmed_hash",
                    },
                ),
                "analysis_draft_issues": list(statistics_draft_issues(draft)),
                "statistical_capabilities": model_visible_capabilities(),
                "selected_external_source": external_summary,
            },
            "context_window_tokens": int(packet.metadata.get("context_window_tokens", 16_384)),
            "max_output_tokens": int(packet.metadata.get("max_output_tokens", 2_048)),
        }
        model_context["work_context"].setdefault("workspace_path", workspace_path)
        result_run_id = request.target_run_id or conversation.active_run_id
        if self.get_result_facts is not None and result_run_id:
            spatial_requested = bool(
                re.search(
                    r"脑区|定位|ROI|解释|解读|结果分析|分析.{0,12}结果|interpret|localiz|region",
                    request.content,
                    re.IGNORECASE,
                )
            )
            model_context["work_context"]["analysis_result_facts"] = {
                "untrusted_content": True,
                "results": self.get_result_facts(result_run_id, spatial_requested),
            }
        dependency = latest_analysis_dependency(conversation)
        if dependency is not None:
            model_context["work_context"]["analysis_dependency"] = {
                key: dependency.get(key)
                for key in (
                    "task_id",
                    "analysis_request",
                    "status",
                    "data_references",
                    "evidence_hash",
                )
            }
            references = dependency.get("data_references")
            if isinstance(references, dict):
                model_context["work_context"]["analysis_dependency"]["data_references"] = (
                    handoff_reference_summary(references)
                )
        counter = RegexTokenCounter()
        budget = int(packet.metadata.get("token_budget", 0))
        while counter.count(model_context) > budget and model_context["recent_messages"]:
            model_context["recent_messages"].pop(0)
        if counter.count(model_context) > budget:
            return reply(
                "当前模型上下文窗口不足，分析草稿已保留。", {"analysis_draft": saved or {}}
            )

        loop_metadata: dict[str, Any] | None = None
        try:
            raw, metadata, loop_metadata = await self._interpret(
                model_context,
                preferred_profile_id=preferred_profile_id,
                model=request.model,
                execution_context=AgentExecutionContext(
                    conversation_id=conversation.conversation_id,
                    project_id=project_id,
                    allowed_run_ids=frozenset(
                        value
                        for value in (request.target_run_id, conversation.active_run_id)
                        if value
                    ),
                    selected_profile_id=preferred_profile_id,
                    context_window_tokens=int(model_context["context_window_tokens"]),
                    max_output_tokens=int(model_context["max_output_tokens"]),
                    workspace_path=workspace_path,
                    workspace_file_scope=(
                        request.workspace_file_scope.paths if request.workspace_file_scope else ()
                    ),
                    workspace_content_consent=workspace_content_consent,
                    work_agent_role=WorkAgentRole.ANALYSIS.value,
                ),
            )
            if metadata.get("stop_reason") in {"provider_timeout", "budget_time"}:
                raise ApplicationError(
                    "work_model_timeout", "数据分析 Agent 请求超时。", status_code=503
                )
            if metadata.get("stop_reason") in {"provider_error", "cancelled"}:
                raise ApplicationError(
                    "work_model_unavailable", "数据分析 Agent 暂时不可用。", status_code=503
                )
            if metadata.get("stop_reason") in {"budget_steps", "budget_tokens"}:
                raise ApplicationError(
                    "work_model_budget_exhausted", "本轮模型预算已用完，统计草稿和证据已保留。"
                )
            if metadata.get("finish_reason") == "length":
                raise ApplicationError("work_response_truncated", "数据分析回复不完整。")
            interpreted = WorkInterpretation.model_validate_json(raw)
            payload: dict[str, Any] = {
                "agent_role": WorkAgentRole.ANALYSIS.value,
                "agent_name": resolve_work_agent(WorkAgentRole.ANALYSIS).display_name,
                "agent_model": metadata,
                "analysis_draft": {
                    "draft": draft.model_dump(mode="json"),
                    "hash": statistics_draft_hash(draft),
                    "version": _snapshot_int(saved, "version") + 1,
                    "issues": list(statistics_draft_issues(draft)),
                    "review": render_statistics_draft_review(draft),
                },
            }
            if saved is not None and interpreted.action in {
                "conversation",
                "get_statistical_results",
                "get_progress",
                "get_qc_status",
            }:
                payload["analysis_draft"] = saved
            if not work_agent_allows_action(WorkAgentRole.ANALYSIS, interpreted.action):
                return reply("该操作不属于数据分析 Agent 的职责范围，未执行任何操作。", payload)
            if loop_metadata is not None:
                payload["tool_loop"] = {
                    **loop_metadata,
                    "agent_role": WorkAgentRole.ANALYSIS.value,
                }
            if interpreted.draft_patch or interpreted.workspace_edits:
                return reply(
                    "数据分析 Agent 返回了预处理专属内容，已拒绝本轮草稿变更。请重新描述分析需求。",
                    payload,
                )
            if interpreted.action == "request_preprocessing_handoff":
                if not project_id or self.prepare_preprocessing_handoff is None:
                    return reply("请先关联工作项目，分析草稿已保留。", payload)
                proof = self.prepare_preprocessing_handoff(
                    conversation_id=conversation.conversation_id,
                    project_id=project_id,
                    run_id=None
                    if external_id
                    else preprocessing_source_run(conversation, request.target_run_id),
                    external_import_id=external_id,
                    reason=interpreted.handoff_reason or interpreted.answer,
                    required_outputs=interpreted.required_preprocessing_outputs,
                )
                payload["agent_handoff"] = proof.model_dump(mode="json")
                if proof.status == "blocked":
                    return reply(
                        "本会话已达到指标需求交接次数上限，草稿和证据已保留，请先核对当前任务。"
                        if "work_handoff_limit_reached" in proof.blocking_items
                        else "上游指标需求已保留，但来源证据已变化或无法核实，请重新检查数据。",
                        payload,
                    )
                return reply(
                    "当前分析缺少必要的已登记指标，已整理需要预处理 Agent 补充的内容。你可以切换到预处理 Agent 继续。",
                    payload,
                )
            if interpreted.action == "prepare_statistical_analysis":
                if not interpreted.analysis_patch:
                    return reply("请提供检验类型或分析目标，我会据此整理独立的统计草稿。", payload)
                updated = merge_statistics_draft(draft, interpreted.analysis_patch)
                updated = updated.model_copy(
                    update={
                        "source_run_id": None,
                        "qc_review_revision_id": None,
                        "qc_review_hash": None,
                        "qc_skip_id": None,
                        "qc_skip_hash": None,
                        "input_manifest_hash": None,
                        "demographics_revision_id": None,
                        "demographics_hash": None,
                        "confirmed_hash": None,
                    }
                )
                issues = list(statistics_draft_issues(updated))
                payload["draft_issues"] = issues
                run_id = (
                    None
                    if external_id
                    else preprocessing_source_run(conversation, request.target_run_id)
                )
                if not issues:
                    if run_id is None and external_id is None:
                        payload["statistical_design_issues"] = [
                            "统计草稿已完整；请先选择一份已成功运行并通过 QC 或记录 QC 跳过的来源运行。"
                        ]
                    elif self.prepare_statistical_proposal is None:
                        payload["statistical_design_issues"] = [
                            "当前服务未连接统计设计装配器，草稿已保留。"
                        ]
                    else:
                        source_args = (
                            {"external_import_id": external_id, "project_id": project_id}
                            if external_id
                            else {"run_id": run_id}
                        )
                        proposal, design_issues = self.prepare_statistical_proposal(
                            draft=updated, **source_args
                        )
                        payload["statistical_design_issues"] = [
                            issue.message for issue in design_issues
                        ]
                        if proposal is not None:
                            updated = updated.model_copy(
                                update={
                                    "source_run_id": proposal.source_run_id,
                                    "external_import_id": proposal.design.external_import_id,
                                    "external_import_hash": proposal.design.external_import_hash,
                                    "qc_review_revision_id": proposal.design.qc_review_revision_id,
                                    "qc_review_hash": proposal.design.qc_review_hash,
                                    "qc_skip_id": proposal.design.qc_skip_id,
                                    "qc_skip_hash": proposal.design.qc_skip_hash,
                                    "input_manifest_hash": proposal.input_manifest_hash,
                                    "demographics_revision_id": proposal.demographics_revision_id,
                                    "demographics_hash": proposal.demographics_hash,
                                }
                            )
                            payload["statistical_design_proposal"] = proposal.model_dump(
                                mode="json"
                            )
                saved_draft = {
                    "draft": updated.model_dump(mode="json"),
                    "hash": statistics_draft_hash(updated),
                    "version": _snapshot_int(saved, "version") + 1,
                    "issues": issues,
                    "review": render_statistics_draft_review(updated),
                    "source_run_id": updated.source_run_id,
                    "input_manifest_hash": updated.input_manifest_hash,
                    "demographics_revision_id": updated.demographics_revision_id,
                    "demographics_hash": updated.demographics_hash,
                }
                payload["analysis_draft"] = saved_draft
                if issues:
                    answer = "统计分析草稿已更新，请补齐草稿中尚未确定的检验和参数信息。"
                elif payload.get("statistical_design_proposal"):
                    answer = (
                        "统计草稿已绑定所选来源的纳入凭据、受试者顺序及登记指标，服务端形成了可审查的设计。"
                        "请核对统计设计、校正参数和人口学来源，再进入统计方案审批。"
                    )
                else:
                    answer = (
                        "统计草稿已完整，但当前来源或登记数据尚不能组成有效设计。"
                        "请查看阻断项并补齐分组、指标或 QC 证据。"
                    )
                return reply(answer, payload)
            target = dispatch_target(interpreted.action)
            if target is not None:
                result = self.coordinator.execute(
                    request.model_copy(
                        update={"action": target, "real_execution_confirmed": False}
                    ),
                    workspace_path=workspace_path,
                    project_id=project_id,
                    active_run_id=conversation.active_run_id,
                    idempotency_key=idempotency_key,
                    conversation_id=conversation.conversation_id,
                )
                return replace(result, payload={**payload, **result.payload})
            if interpreted.action not in {
                "conversation",
                "get_statistical_results",
                "get_progress",
                "get_qc_status",
            }:
                return reply("该动作不属于数据分析 Agent 的职责范围，未执行任何操作。", payload)
            return reply(interpreted.answer, payload)
        except (ApplicationError, ValidationError, ValueError) as exc:
            diagnostic, reason = _work_failure_details(exc, {}, "analysis_response")
            return reply(
                "数据分析 Agent 未能完成本轮请求；已有统计草稿和工作流状态仍保留。\n\n" + reason,
                {
                    "model_error": diagnostic,
                    **({"tool_loop": loop_metadata} if loop_metadata else {}),
                },
            )

    @staticmethod
    def _parses(content: str) -> bool:
        try:
            WorkInterpretation.model_validate_json(content)
        except (ValidationError, ValueError):
            return False
        return True

    def _compile(
        self,
        conversation: ConversationView,
        request: ConversationTurnCreate,
        draft: WorkDraft,
        workspace_path: str | None,
        project_id: str | None,
        idempotency_key: str,
        *,
        proposal: bool = False,
        dataset_id: str | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> WorkActionResult:
        """Render a user-confirmed draft into a reviewable plan.

        All workspace/dataset/manifest binding happens inside the coordinator, so
        the dialogue layer only decides *which* dataset the conversation is bound
        to and how the result is presented back to the user.
        """

        dataset_data = latest_payload(conversation, "dataset")
        evidence = (
            snapshot
            or latest_agent_payload(conversation, "work_draft", WorkAgentRole.PREPROCESSING)
            or {}
        )
        if evidence.get("input_evidence_stale") or evidence.get("input_manifest_stale"):
            return WorkActionResult(
                "输入文件已变化，请先重新读取并核实采集信息，再生成方案。",
                payload={"work_draft": evidence, "confirmation_request": {"options": []}},
                workspace_path=workspace_path,
                project_id=project_id,
                active_run_id=conversation.active_run_id,
            )
        dataset_id = dataset_id or (dataset_data.get("dataset_id") if dataset_data else None)
        if not isinstance(dataset_id, str):
            skill = latest_payload(conversation, "skill_plan")
            ref = skill.get("dataset_ref") if skill else None
            dataset_id = ref if isinstance(ref, str) else None

        result = self.coordinator.prepare_plan_from_draft(
            request,
            draft,
            workspace_path=workspace_path,
            project_id=project_id,
            dataset_id=dataset_id,
            conversation_id=conversation.conversation_id,
            user_confirmation_ref=(
                f"conversation:{conversation.conversation_id}:turn:{conversation.version + 1}"
            ),
            idempotency_key=idempotency_key,
            proposal=proposal,
        )
        payload = dict(result.payload)
        payload["work_draft"] = {
            **(
                snapshot
                or latest_agent_payload(conversation, "work_draft", WorkAgentRole.PREPROCESSING)
                or {}
            ),
            "draft": draft.model_dump(mode="json"),
            "hash": draft_hash(draft),
            "workspace_path": workspace_path,
            "compiled": "plan_revision" in payload,
        }
        payload["draft_issues"] = list(review_issues(draft, payload["work_draft"].get("facts")))
        payload["plan_preview"] = plan_preview(draft)
        payload["planning_decisions"] = planning_decisions(
            draft, payload["work_draft"].get("sources", {})
        )
        if "plan_revision" not in payload:
            # Binding or validation refused; keep the uncompiled draft so the
            # user can correct it and confirm again.
            return replace(
                result,
                payload=payload,
                workspace_path=workspace_path,
                active_run_id=conversation.active_run_id,
            )
        plan = payload["plan_revision"]
        qc_mode = (
            QcExecutionMode.REQUIRE
            if draft.qc_mode == "head_motion"
            else QcExecutionMode.SKIP
            if draft.qc_mode == "skip"
            else None
        )
        payload["confirmation_request"] = self.run_confirmation_request(
            conversation_id=conversation.conversation_id,
            project_id=str(result.project_id or project_id),
            plan_revision_id=str(plan["plan_revision_id"]),
            plan_hash=str(plan["plan_hash"]),
            qc_mode=qc_mode,
            summary=execution_summary(draft),
        )
        return replace(
            result,
            payload=payload,
            workspace_path=workspace_path,
            active_run_id=conversation.active_run_id,
            assistant_content="方案已通过科学校验，请审查关键参数及执行流程，再确认启动计算。",
        )
