"""Model interpretation produces drafts; only server-checked commands authorize actions."""

# ruff: noqa: RUF001
from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from neuroagent.agent.gateway import ChatGatewayResult
from neuroagent.application.contracts import (
    ConversationAction,
    ConversationTurnCreate,
    ConversationView,
    QcExecutionMode,
)
from neuroagent.application.conversation_work import ConversationWorkCoordinator, WorkActionResult
from neuroagent.application.errors import ApplicationError
from neuroagent.application.work_actions import (
    SERVER_ONLY_ACTIONS,
    dispatch_target,
    is_model_selectable,
)
from neuroagent.application.work_draft import (
    WorkDraft,
    draft_hash,
    draft_issues,
    draft_schema,
    merge_work_draft,
    render_draft_review,
)
from neuroagent.application.work_tools import AgentExecutionContext, WorkToolRunner
from neuroagent.context.engine import RegexTokenCounter
from neuroagent.context.interfaces import ContextPacket

logger = logging.getLogger(__name__)


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


def execution_command(plan_hash: str, qc: str, confirmation_id: str | None = None) -> str:
    suffix = f" {confirmation_id}" if confirmation_id else ""
    return f"批准并启动 {plan_hash[:12]} {qc}{suffix}"


def _work_loop_metadata(looped: Any) -> dict[str, Any]:
    """Persist loop outcomes even when the final answer requires fallback."""

    return {
        "enabled": True,
        "stop_reason": looped.stop_reason,
        "steps": looped.steps,
        "tool_names": list(looped.tool_names),
        "usage": looped.usage,
        "fallback_candidate": not bool(looped.final_text.strip()),
        "fallback_used": False,
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


# The user echoes this exact, server-issued command; a model cannot forge it
# because authorization is never taken from model output.  The pattern and
# ``execution_command`` must stay in step.
EXECUTION_COMMAND_PATTERN = r"批准并启动 ([a-f0-9]{12}) (做头动QC|跳过QC) ([a-f0-9-]{36})"


class WorkDialogue:
    def __init__(
        self,
        coordinator: ConversationWorkCoordinator,
        generate: Callable[..., Awaitable[ChatGatewayResult]],
        tool_runner: WorkToolRunner | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.generate = generate
        # Optional: without it Work keeps the single-shot interpretation path.
        self.tool_runner = tool_runner

    async def respond(
        self,
        conversation: ConversationView,
        request: ConversationTurnCreate,
        packet: ContextPacket,
        *,
        workspace_path: str | None,
        project_id: str | None,
        preferred_profile_id: str | None,
        idempotency_key: str,
        has_profiles: bool,
    ) -> WorkActionResult:
        def reply(text: str, payload: dict[str, Any] | None = None) -> WorkActionResult:
            return WorkActionResult(
                text,
                payload=payload or {},
                workspace_path=workspace_path,
                project_id=project_id,
                active_run_id=conversation.active_run_id,
            )

        saved = latest_payload(conversation, "work_draft")
        # A start confirmation is single use.  It is read before the workspace
        # match so that re-selecting a workspace cannot resurrect a used one.
        start_confirmation_used = bool(saved and saved.get("consumed"))
        if saved and saved.get("workspace_path") != workspace_path:
            saved = None
        draft = WorkDraft.model_validate(saved["draft"]) if saved else WorkDraft()
        text = request.content.strip()
        if text.startswith("确认方案参数"):
            if not saved or text != f"确认方案参数 {draft_hash(draft)[:12]}":
                return reply("草稿已变化或工作区不匹配，请先查看当前参数草稿并确认最新版本。")
            if saved.get("compiled"):
                return reply("该参数草稿已经编译，请审查方案后单独确认运行。")
            issues = draft_issues(draft)
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
            if start_confirmation_used:
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
                            "draft": (saved or {}).get("draft") or {},
                            "hash": (saved or {}).get("hash"),
                            "workspace_path": (saved or {}).get("workspace_path") or workspace_path,
                            "compiled": False,
                            "consumed": True,
                            "consumed_run_id": result.active_run_id,
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

        try:
            model_context: dict[str, Any] = {
                "question": request.content,
                "recent_messages": [
                    {"role": m.role, "content": m.content} for m in packet.recent_messages
                ],
                "pinned_context": [item.model_dump() for item in packet.pinned_context],
                "conversation_summary": packet.conversation_summary,
                "work_context": {
                    **packet.work_context,
                    "parameter_draft": draft.model_dump(mode="json", exclude={"confirmed_hash"}),
                    "draft_schema": draft_schema(),
                },
                "context_window_tokens": int(packet.metadata.get("context_window_tokens", 16_384)),
                "max_output_tokens": int(packet.metadata.get("max_output_tokens", 2_048)),
            }
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
                    context_window_tokens=int(
                        packet.metadata.get("context_window_tokens", 16_384)
                    ),
                    max_output_tokens=int(packet.metadata.get("max_output_tokens", 2_048)),
                ),
            )
            interpreted = WorkInterpretation.model_validate_json(raw_content)
            if interpreted.draft_patch and interpreted.action != "prepare_preprocessing_plan":
                raise ValueError("draft patch requires planning intent")
            payload: dict[str, Any] = {"model": metadata}
            if loop_metadata is not None:
                payload["tool_loop"] = loop_metadata
            if interpreted.action == "prepare_preprocessing_plan":
                draft = merge_work_draft(draft, interpreted.draft_patch)
                issues = draft_issues(draft)
                review = render_draft_review(draft)
                payload["work_draft"] = {
                    "draft": draft.model_dump(mode="json"),
                    "hash": draft_hash(draft),
                    "workspace_path": workspace_path,
                    "compiled": False,
                }
                if issues:
                    suffix = "\n\n仍需补充或修正：\n" + "\n".join(issues)
                else:
                    suffix = (
                        "\n\n请审查以上全部参数。确认后将登记当前工作区并校验编译方案，尚不启动计算。"
                        f"\n请回复：确认方案参数 {draft_hash(draft)[:12]}"
                    )
                return reply(interpreted.answer + "\n\n" + review + suffix, payload)
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
                return reply(
                    "启动计算需要单独确认当前方案及 QC 选择。请审查方案后回复以下一种：\n"
                    + execution_command(plan_hash, "做头动QC", require_id)
                    + "\n"
                    + execution_command(plan_hash, "跳过QC", skip_id),
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
        except (ApplicationError, ValidationError, ValueError):
            return reply(
                "本轮模型服务不可用或返回的结构不符合要求，未更改参数草稿，也未执行任务。"
                "请检查模型连接后重试；原有对话和草稿仍保留。"
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

        loop_metadata: dict[str, Any] | None = None
        if self.tool_runner is not None and await self.tool_runner.available(preferred_profile_id):
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
            )
            loop_metadata = _work_loop_metadata(looped)
            if looped.final_text.strip():
                metadata = {
                    "profile_id": looped.profile_id,
                    "model": looped.model,
                    "context_hash": looped.context_hash,
                    "redaction_count": looped.redaction_count,
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
        generated = await self.generate(
            **fallback_context,
            evidence=[],
            preferred_profile_id=preferred_profile_id,
            model=model,
            allow_web_search=False,
            work_mode=True,
        )
        metadata = {
            "profile_id": generated.selected_profile_id,
            "model": generated.response.model,
            "context_hash": generated.context_hash,
            "redaction_count": generated.redaction_count,
        }
        if loop_metadata is not None:
            loop_metadata = {
                **loop_metadata,
                "fallback_used": True,
                "fallback_reason": "tool_loop_unusable_or_incomplete",
            }
        return generated.response.content, metadata, loop_metadata

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
    ) -> WorkActionResult:
        """Render a user-confirmed draft into a reviewable plan.

        All workspace/dataset/manifest binding happens inside the coordinator, so
        the dialogue layer only decides *which* dataset the conversation is bound
        to and how the result is presented back to the user.
        """

        dataset_data = latest_payload(conversation, "dataset")
        dataset_id = dataset_data.get("dataset_id") if dataset_data else None
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
        )
        payload = dict(result.payload)
        payload["work_draft"] = {
            "draft": draft.model_dump(mode="json"),
            "hash": draft_hash(draft),
            "workspace_path": workspace_path,
            "compiled": "plan_revision" in payload,
        }
        if "plan_revision" not in payload:
            # Binding or validation refused; keep the uncompiled draft so the
            # user can correct it and confirm again.
            return replace(
                result,
                payload=payload,
                workspace_path=workspace_path,
                active_run_id=conversation.active_run_id,
            )
        return replace(
            result,
            payload=payload,
            workspace_path=workspace_path,
            active_run_id=conversation.active_run_id,
            assistant_content=result.assistant_content
            + "\n\n"
            + render_draft_review(draft)
            + "\n\n本次仅生成方案。确认启动时请单独回复：\n"
            + "请先让系统签发一次性确认指令，再原样回复该指令。",
        )
