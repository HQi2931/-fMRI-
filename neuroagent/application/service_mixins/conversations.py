"""Persistent Chat/Work conversations with deterministic tool orchestration."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from neuroagent.analysis.models import EvidenceChunk, RsFmriAnswer
from neuroagent.application.automation.analysis_outbox import (
    AnalysisOutboxRegistrationSource,
    AnalysisOutboxRetryRequest,
    AnalysisOutboxStatus,
)
from neuroagent.application.automation.composite_goal import CompositeGoalIdentity
from neuroagent.application.automation.goal_selection import analysis_selection
from neuroagent.application.automation.result_interpretation import (
    ResultInterpretationRetryRequest,
    ResultInterpretationStatus,
)
from neuroagent.application.automation.service import WorkTaskApplicationService
from neuroagent.application.contracts.agent import (
    ModelProfileView,
    RsFmriAnswerView,
    RsFmriQuestionRequest,
)
from neuroagent.application.contracts.conversations import (
    CompositeGoalSelection,
    ConversationAction,
    ConversationContextView,
    ConversationCreate,
    ConversationMessageView,
    ConversationMode,
    ConversationRole,
    ConversationToolCallView,
    ConversationToolStatus,
    ConversationTurnCreate,
    ConversationTurnView,
    ConversationView,
    ConversationWorkTaskView,
    MemoryCreate,
    MemoryUpdate,
    MemoryView,
    WorkAgentSelection,
    WorkConfirmationCreate,
    WorkDraftUpdate,
    WorkReviewCreate,
)
from neuroagent.application.contracts.external_imports import ExternalAnalysisImportView
from neuroagent.application.contracts.plans import PlanRevisionView
from neuroagent.application.contracts.runs import (
    QcExecutionMode,
    RunCreate,
    RunView,
)
from neuroagent.application.conversation_context import (
    ContextBudgetError,
    ConversationContextCoordinator,
)
from neuroagent.application.conversation_work import (
    ConversationWorkCoordinator,
    WorkActionResult,
)
from neuroagent.application.errors import (
    ApplicationError,
    ConflictError,
    InputValidationError,
    NotFoundError,
)
from neuroagent.application.hashing import content_hash
from neuroagent.application.repository_ports import ConversationRepositoryPort
from neuroagent.application.service_mixins._base import BaseServiceMixin
from neuroagent.application.work_agents import (
    handoff_identity,
    work_agent_role_for_turn,
)
from neuroagent.application.work_dialogue import (
    STAGE_CONFIRMATION_PATTERN,
    WorkDialogue,
    latest_payload,
)
from neuroagent.application.work_draft import (
    WorkDraft,
    draft_hash,
    draft_snapshot,
    execution_summary,
    invalidate_input_evidence,
    merge_work_draft,
    plan_preview,
    planning_decisions,
    render_draft_review,
    review_issues,
    validate_draft_patch,
)
from neuroagent.application.work_tasks import run_for_agent_context
from neuroagent.application.work_tasks import work_task as conversation_work_task
from neuroagent.chat.agent import ChatAgent
from neuroagent.chat.interfaces import ChatAgentError
from neuroagent.chat.models import ChatAgentRequest
from neuroagent.context.interfaces import ContextPacket
from neuroagent.memory.models import MemoryCandidateDraft
from neuroagent.multi_agent.registry import WorkAgentRole
from neuroagent.observability.work_timings import capture_work_timings

logger = logging.getLogger(__name__)
MAX_ANALYSIS_OUTBOX_ATTEMPTS = 5
MAX_RESULT_INTERPRETATION_ATTEMPTS = 5
RECOVERABLE_ANALYSIS_OUTBOX_ERRORS = (
    "analysis_model_profile_missing",
    "analysis_model_retry_exhausted",
    "analysis_outbox_retry_exhausted",
)
RECOVERABLE_RESULT_INTERPRETATION_ERRORS = (
    "result_interpretation_model_profile_missing",
    "result_interpretation_model_retry_exhausted",
    "result_interpretation_retry_exhausted",
)


def _verify_analysis_only_external_source(
    goal: CompositeGoalIdentity, imported: ExternalAnalysisImportView
) -> None:
    """Compare every mutable source revision field with the immutable goal root."""
    manifest_hash = imported.selection.input_manifest_hash if imported.selection else None
    tree_hash = imported.inspection.source_tree_hash
    revision_hash = content_hash(
        {
            "external_import_id": imported.import_id,
            "import_version": imported.version,
            "source_tree_hash": tree_hash,
            "inclusion_hash": imported.inclusion_hash,
            "input_manifest_hash": manifest_hash,
        }
    )
    if (
        goal.goal_kind != "analysis_only_external_import"
        or goal.source_external_import_id != imported.import_id
        or goal.project_id != imported.project_id
        or imported.status != "included"
        or imported.selection is None
        or imported.inclusion_hash is None
        or tree_hash is None
        or imported.version != goal.source_import_version
        or tree_hash != goal.source_tree_hash
        or imported.inclusion_hash != goal.source_inclusion_hash
        or manifest_hash != goal.source_manifest_hash
        or revision_hash != goal.source_revision_hash
        or imported.inspection.metric_kind is None
        or imported.inspection.scaling is None
    ):
        raise ConflictError(
            "analysis_goal_external_source_changed",
            "当前外部指标来源与所选分析目标冻结的项目、纳入状态或 revision 不一致。",
        )


def _analysis_goal_original_request(
    conversation: ConversationView,
    goal: CompositeGoalIdentity,
    *,
    current_request: str,
) -> str:
    """Recover the immutable original request from exact assistant evidence."""
    matches: set[str] = set()
    for message in conversation.messages:
        if message.role is not ConversationRole.ASSISTANT:
            continue
        dependency = message.payload.get("analysis_dependency")
        if not isinstance(dependency, dict) or dependency.get("task_id") != goal.intent_task_id:
            continue
        original = dependency.get("analysis_request")
        if (
            dependency.get("task_kind") != goal.goal_kind
            or dependency.get("conversation_id") != goal.conversation_id
            or dependency.get("project_id") != goal.project_id
            or not isinstance(original, str)
            or content_hash(original.strip()) != goal.analysis_request_hash
        ):
            raise ConflictError(
                "analysis_goal_identity_conflict",
                "当前目标的原始请求证据与其稳定摘要不匹配。",
            )
        matches.add(original.strip())
    if len(matches) == 1:
        return next(iter(matches))
    if not matches and content_hash(current_request.strip()) == goal.analysis_request_hash:
        return current_request.strip()
    raise ConflictError(
        "analysis_goal_identity_conflict",
        "无法从当前会话恢复唯一且可核验的目标原始请求。",
    )


class _CompositeGoalModelBudgetSession:
    """Bind successive provider turns to one exact fenced outbox attempt."""

    def __init__(
        self,
        automation: WorkTaskApplicationService,
        *,
        intent_task_id: str,
        scope: str,
    ) -> None:
        self._automation = automation
        self._intent_task_id = intent_task_id
        self._scope = scope
        self._call_index = 0

    def reserve(self, estimated_tokens: int, estimated_elapsed_seconds: int) -> Any:
        self._call_index += 1
        call_id = content_hash({"scope": self._scope, "call_index": self._call_index})
        reservation_id = content_hash({"intent_task_id": self._intent_task_id, "call_id": call_id})
        return self._automation.reserve_composite_goal_model_call(
            self._intent_task_id,
            reservation_id=reservation_id,
            call_id=call_id,
            estimated_tokens=estimated_tokens,
            estimated_elapsed_seconds=estimated_elapsed_seconds,
        )

    def settle(
        self,
        reserve_result: Any,
        *,
        actual_tokens: int | None,
        elapsed_seconds: int,
    ) -> Any:
        reservation = getattr(reserve_result, "reservation", None)
        reservation_id = getattr(reservation, "reservation_id", None)
        if not isinstance(reservation_id, str):
            raise ValueError("model call reservation identity is missing")
        return self._automation.settle_composite_goal_model_call(
            self._intent_task_id,
            reservation_id=reservation_id,
            actual_tokens=actual_tokens,
            elapsed_seconds=elapsed_seconds,
        )


class ConversationMixin(BaseServiceMixin):
    repository: ConversationRepositoryPort
    conversation_context: ConversationContextCoordinator
    conversation_work: ConversationWorkCoordinator
    work_dialogue: WorkDialogue
    chat_agent: ChatAgent
    workspace_content_consent_granted: Callable[..., bool]
    get_plan: Callable[[str], PlanRevisionView]
    get_run: Callable[[str], RunView]
    list_model_profiles: Callable[[], list[ModelProfileView]]
    get_external_import: Callable[[str], ExternalAnalysisImportView]
    automation: WorkTaskApplicationService

    @staticmethod
    async def _await_fenced_generation(
        generation: Awaitable[Any], claim_lost: asyncio.Event
    ) -> Any | None:
        generation_task = asyncio.ensure_future(generation)
        claim_lost_task = asyncio.create_task(claim_lost.wait())
        try:
            await asyncio.wait(
                {generation_task, claim_lost_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if claim_lost.is_set():
                generation_task.cancel()
                await asyncio.gather(generation_task, return_exceptions=True)
                return None
            return await generation_task
        finally:
            claim_lost_task.cancel()
            await asyncio.gather(claim_lost_task, return_exceptions=True)

    def select_composite_goal(
        self,
        conversation_id: str,
        intent_task_id: str,
        request: CompositeGoalSelection,
        idempotency_key: str,
    ) -> ConversationView:
        def action() -> ConversationView:
            conversation = self.repository.get_conversation(conversation_id)
            goal = self.automation.get_composite_goal(conversation_id, intent_task_id)
            if goal.goal_kind == "analysis_only_external_import":
                try:
                    imported = self.get_external_import(goal.source_external_import_id or "")
                except ApplicationError as exc:
                    raise ConflictError(
                        "analysis_goal_external_source_changed",
                        "当前外部指标来源无法核验，不能恢复此分析目标。",
                    ) from exc
                _verify_analysis_only_external_source(goal, imported)
            progress = self.automation.get_composite_goal_progress(conversation_id, intent_task_id)
            if progress.status.value not in {
                "waiting_user",
                "waiting_analysis",
                "waiting_result_interpretation",
                "completed",
            }:
                raise ConflictError(
                    "analysis_goal_not_selectable", "当前目标状态或权威证据尚不可恢复。"
                )
            outbox = (
                None
                if goal.goal_kind == "analysis_only_external_import"
                else self.automation.get_analysis_outbox(intent_task_id)
            )
            task = (
                self.automation.get_task(goal.analysis_work_task_id)
                if goal.analysis_work_task_id is not None
                else None
            )
            try:
                plan = (
                    self.get_plan(task.authority_refs.plan_revision_id)
                    if task is not None and task.authority_refs.plan_revision_id is not None
                    else None
                )
                run = (
                    self.get_run(task.authority_refs.workflow_run_id)
                    if task is not None and task.authority_refs.workflow_run_id is not None
                    else None
                )
            except ApplicationError as exc:
                raise ConflictError(
                    "analysis_goal_selection_conflict", "目标绑定的统计方案或运行无法读取。"
                ) from exc
            selected = self.repository.select_analysis_goal(
                conversation_id,
                project_id=goal.project_id,
                expected_version=request.expected_version,
                task=analysis_selection(conversation, goal, outbox, task, plan=plan, run=run),
            )
            self.event_store.append_event(
                project_id=goal.project_id,
                run_id=None,
                event_type="CompositeAnalysisGoalSelected",
                severity="info",
                payload={"conversation_id": conversation_id, "intent_task_id": intent_task_id},
            )
            return selected

        return self._idempotent(
            scope=f"conversations:{conversation_id}:composite-goals:{intent_task_id}:select",
            key=idempotency_key,
            request=request,
            response_type=ConversationView,
            action=action,
        )

    def resolve_selected_frozen_analysis(
        self, conversation: ConversationView
    ) -> tuple[ConversationWorkTaskView, PlanRevisionView] | None:
        """Recheck the selected frozen identity in a short read-only Unit of Work."""
        selected = conversation_work_task(conversation, WorkAgentRole.ANALYSIS)
        if selected is None:
            return None
        if selected.analysis_intent_task_id is None:
            if selected.plan_revision_id is not None or selected.run_id is not None:
                composite_goal = None
                if selected.work_task_id is not None:
                    composite_goal = self.automation.find_composite_goal_for_analysis_task(
                        selected.work_task_id
                    )
                if selected.work_task_id is None or composite_goal is not None:
                    raise ConflictError(
                        "analysis_goal_selection_conflict", "冻结分析引用缺少目标身份。"
                    )
            return None
        if selected.plan_revision_id is None:
            if selected.run_id is not None:
                raise ConflictError(
                    "analysis_goal_selection_conflict", "统计运行引用缺少冻结方案身份。"
                )
            return None
        with self.idempotency_store.atomic():
            current = self.repository.get_conversation(conversation.conversation_id)
            if (
                current.version != conversation.version
                or conversation_work_task(current, WorkAgentRole.ANALYSIS) != selected
            ):
                raise ConflictError(
                    "analysis_goal_selection_conflict", "当前分析目标在读取期间已变化。"
                )
            intent_id = selected.analysis_intent_task_id
            goal = self.automation.get_composite_goal(current.conversation_id, intent_id)
            progress = self.automation.get_composite_goal_progress(
                current.conversation_id, intent_id
            )
            if progress.status.value not in {
                "waiting_user",
                "waiting_analysis",
                "waiting_result_interpretation",
                "completed",
            }:
                raise ConflictError(
                    "analysis_goal_selection_conflict", "当前目标的权威证据尚不可读取。"
                )
            outbox = (
                None
                if goal.goal_kind == "analysis_only_external_import"
                else self.automation.get_analysis_outbox(intent_id)
            )
            if goal.analysis_work_task_id is None:
                raise ConflictError(
                    "analysis_goal_selection_conflict", "当前目标缺少统计任务身份。"
                )
            task = self.automation.get_task(goal.analysis_work_task_id)
            try:
                plan = self.get_plan(selected.plan_revision_id)
                run = self.get_run(selected.run_id) if selected.run_id is not None else None
            except ApplicationError as exc:
                raise ConflictError(
                    "analysis_goal_selection_conflict", "当前目标绑定的统计方案或运行无法读取。"
                ) from exc
            restored = analysis_selection(current, goal, outbox, task, plan=plan, run=run)
            if restored != selected:
                raise ConflictError(
                    "analysis_goal_selection_conflict", "当前角色引用与冻结目标身份不一致。"
                )
            return restored, plan

    def select_work_agent(
        self,
        conversation_id: str,
        request: WorkAgentSelection,
        idempotency_key: str,
    ) -> ConversationView:
        def action() -> ConversationView:
            selected = self.repository.select_work_agent(conversation_id, request)
            self.event_store.append_event(
                project_id=selected.project_id,
                run_id=selected.active_run_id,
                event_type="WorkAgentSelected",
                severity="info",
                payload={
                    "conversation_id": conversation_id,
                    "agent_role": request.agent_role.value,
                },
            )
            return selected

        return self._idempotent(
            scope=f"conversations:{conversation_id}:work-agent",
            key=idempotency_key,
            request=request,
            response_type=ConversationView,
            action=action,
        )

    async def consume_next_analysis_outbox(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
    ) -> str | None:
        """Prepare one READY analysis draft; model work never holds a DB transaction."""
        claim = self.automation.claim_next_analysis_outbox(worker_id, lease_seconds=lease_seconds)
        if claim is None:
            return None

        outbox = self.automation.get_analysis_outbox(claim.intent_task_id)
        stop_renewal = asyncio.Event()
        claim_lost = asyncio.Event()

        async def renew_claim() -> None:
            interval = max(1.0, lease_seconds / 3)
            while not stop_renewal.is_set():
                await asyncio.sleep(interval)
                if stop_renewal.is_set():
                    return
                try:
                    renewed = self.automation.renew_analysis_outbox_claim(
                        claim, lease_seconds=lease_seconds
                    )
                except Exception:
                    logger.exception("Analysis outbox lease renewal failed")
                    claim_lost.set()
                    return
                if renewed is None:
                    claim_lost.set()
                    return

        heartbeat = asyncio.create_task(renew_claim())

        async def stop_heartbeat() -> None:
            stop_renewal.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

        def defer(error_code: str, *, retry_seconds: int | None = None) -> str:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code):
                error_code = "analysis_outbox_consumer_error"
            delay = retry_seconds or min(300, 5 * 2 ** min(outbox.attempt_count, 6))
            committed = self.automation.defer_analysis_outbox_claim(
                claim,
                retry_after_seconds=delay,
                error_code=error_code,
            )
            return "deferred" if committed else "claim_lost"

        def bounded_waiting_user(*, expected_version: int, error_code: str, content: str) -> str:
            return commit_terminal(
                expected_version=expected_version,
                status=AnalysisOutboxStatus.WAITING_USER,
                content=content,
                payload=outbox_payload(
                    AnalysisOutboxStatus.WAITING_USER,
                    error_code,
                    retry_available=True,
                ),
                error_code=error_code,
                record_attempt=True,
            )

        def outbox_payload(
            status: AnalysisOutboxStatus,
            error_code: str | None = None,
            *,
            retry_available: bool = False,
            include_source: bool = True,
        ) -> dict[str, Any]:
            continuation = (
                {
                    "intent_task_id": outbox.intent_task_id,
                    "retry_generation": outbox.retry_generation,
                    "preprocessing_task_id": outbox.preprocessing_task_id,
                    "run_id": outbox.run_id,
                    "plan_revision_id": outbox.plan_revision_id,
                    "manifest_hash": outbox.manifest_hash,
                    "artifact_ids": list(outbox.artifact_ids),
                }
                if include_source
                else None
            )
            return {
                "agent_role": WorkAgentRole.ANALYSIS.value,
                "agent_name": "数据分析 Agent",
                **(
                    {"analysis_outbox_continuation": continuation}
                    if continuation is not None
                    else {}
                ),
                "analysis_outbox_result": {
                    "status": status.value,
                    **({"error_code": error_code} if error_code else {}),
                    "retry_available": retry_available,
                },
            }

        def commit_terminal(
            *,
            expected_version: int,
            status: AnalysisOutboxStatus,
            content: str,
            payload: dict[str, Any],
            user_content: str | None = None,
            error_code: str | None = None,
            record_attempt: bool = False,
            safe_notice: bool = False,
        ) -> str:
            continuation_key = f"analysis_outbox:{outbox.intent_task_id}:g{outbox.retry_generation}"
            with self.automation.atomic():
                current = self.repository.get_conversation(outbox.conversation_id)
                if current.version != expected_version:
                    raise ConflictError(
                        "analysis_outbox_conversation_changed",
                        "分析续接期间会话发生变化，结果没有提交。",
                    )
                source_matches = (
                    current.mode is ConversationMode.WORK
                    and current.project_id == outbox.project_id
                )
                if not source_matches and not safe_notice:
                    raise ConflictError(
                        "analysis_outbox_conversation_mismatch",
                        "分析续接会话已切换模式或项目。",
                    )
                work_task_id: str | None = None
                if status is AnalysisOutboxStatus.PROPOSAL_READY and source_matches:
                    work_task_id = self._commit_analysis_work_task_binding(
                        conversation=current,
                        user_content=user_content,
                        payload=payload,
                        project_id=outbox.project_id,
                    )
                    if work_task_id is None:
                        raise ConflictError(
                            "analysis_outbox_work_task_binding_failed",
                            "统计提案没有生成可持久化的分析任务。",
                        )
                    result_payload = payload.setdefault("analysis_outbox_result", {})
                    if isinstance(result_payload, dict):
                        result_payload["work_task_id"] = work_task_id
                _, assistant = self.repository.append_assistant_continuation(
                    outbox.conversation_id,
                    continuation_key=continuation_key,
                    assistant_content=content,
                    assistant_payload=payload,
                    project_id=current.project_id,
                    work_task_id=work_task_id,
                    update_work_state=(
                        source_matches and status is not AnalysisOutboxStatus.BLOCKED
                    ),
                )
                self.automation.complete_analysis_outbox(
                    claim,
                    status=status,
                    continuation_message_id=assistant.message_id,
                    analysis_work_task_id=work_task_id,
                    error_code=error_code,
                    record_attempt=record_attempt,
                )
            return status.value

        try:
            conversation = self.repository.get_conversation(outbox.conversation_id)
            if (
                conversation.mode is not ConversationMode.WORK
                or conversation.project_id != outbox.project_id
            ):
                await stop_heartbeat()
                return commit_terminal(
                    expected_version=conversation.version,
                    status=AnalysisOutboxStatus.BLOCKED,
                    content=(
                        "当前会话的工作区已切换，原自动续接已停止。"
                        "请回到原项目，在数据分析 Agent 中重新发起请求。"
                    ),
                    payload={
                        "agent_role": WorkAgentRole.ANALYSIS.value,
                        "analysis_outbox_result": {
                            "status": AnalysisOutboxStatus.BLOCKED.value,
                            "error_code": "analysis_outbox_conversation_mismatch",
                            "retry_available": False,
                        },
                    },
                    error_code="analysis_outbox_conversation_mismatch",
                    safe_notice=True,
                )

            try:
                source = self.automation.verify_analysis_outbox_source(outbox)
            except ApplicationError as exc:
                if exc.code == "analysis_outbox_authority_indeterminate":
                    await stop_heartbeat()
                    if outbox.attempt_count + 1 >= MAX_ANALYSIS_OUTBOX_ATTEMPTS:
                        return bounded_waiting_user(
                            expected_version=conversation.version,
                            error_code="analysis_outbox_retry_exhausted",
                            content="预处理证据连续多次无法稳定读取，自动续接已暂停。确认数据库与运行证据恢复可读后，可手动重试；系统不会替你创建统计方案或运行。",
                        )
                    return defer(exc.code)
                return commit_terminal(
                    expected_version=conversation.version,
                    status=AnalysisOutboxStatus.BLOCKED,
                    content=(
                        "预处理来源证据目前无法核实，系统已停止自动分析。"
                        "请检查原运行的冻结方案、QC 决定和指标产物后再继续。"
                    ),
                    payload=outbox_payload(AnalysisOutboxStatus.BLOCKED, exc.code),
                    error_code=exc.code,
                )

            prepare_analysis_handoff = self.work_dialogue.prepare_analysis_handoff
            if prepare_analysis_handoff is None:
                return commit_terminal(
                    expected_version=conversation.version,
                    status=AnalysisOutboxStatus.BLOCKED,
                    content="当前服务缺少分析来源核验能力，自动续接已停止。请联系管理员检查服务配置。",
                    payload=outbox_payload(
                        AnalysisOutboxStatus.BLOCKED,
                        "analysis_handoff_unavailable",
                    ),
                    error_code="analysis_handoff_unavailable",
                )
            proof = prepare_analysis_handoff(source.run_id, source.project_id, verify_files=True)
            metric_ids = {
                str(item["artifact_id"])
                for item in proof.data_references.get("metrics", [])
                if isinstance(item, dict) and isinstance(item.get("artifact_id"), str)
            }
            mask_ids = {
                str(item["artifact_id"])
                for item in proof.data_references.get("masks", [])
                if isinstance(item, dict) and isinstance(item.get("artifact_id"), str)
            }
            allowed_ids = set(source.artifact_ids) | set(source.dependency_artifact_ids)
            if (
                proof.status != "ready"
                or not metric_ids
                or not metric_ids.issubset(set(source.artifact_ids))
                or not mask_ids
                or not mask_ids.issubset(allowed_ids)
                or not set(source.dependency_artifact_ids).issubset(mask_ids)
            ):
                return commit_terminal(
                    expected_version=conversation.version,
                    status=AnalysisOutboxStatus.BLOCKED,
                    content=(
                        "已登记的指标或其 mask 谱系不属于本次已核验来源，自动分析已停止。"
                        "请检查来源产物和 QC 证据。"
                    ),
                    payload=outbox_payload(
                        AnalysisOutboxStatus.BLOCKED,
                        "analysis_outbox_handoff_artifact_mismatch",
                    ),
                    error_code="analysis_outbox_handoff_artifact_mismatch",
                )

            if not self.list_model_profiles():
                await stop_heartbeat()
                return commit_terminal(
                    expected_version=conversation.version,
                    status=AnalysisOutboxStatus.WAITING_USER,
                    content="当前没有配置可用的模型，分析草稿尚未生成。请先在设置中配置并测试模型，然后点击本消息中的“重新尝试准备草稿”；系统会重新核验原始来源，不会启动统计运行。",
                    payload=outbox_payload(
                        AnalysisOutboxStatus.WAITING_USER,
                        "analysis_model_profile_missing",
                        retry_available=True,
                    ),
                    error_code="analysis_model_profile_missing",
                )

            context_window, max_output = self.conversation_context.context_limits(
                conversation.preferred_profile_id
            )
            packet = ContextPacket(
                question=source.analysis_request,
                work_context={
                    "project_id": source.project_id,
                    "agent_role": WorkAgentRole.ANALYSIS.value,
                    "analysis_source_evidence": source.model_facts.model_dump(mode="json"),
                },
                metadata={
                    "context_window_tokens": context_window,
                    "max_output_tokens": max_output,
                    "token_budget": int((context_window - max_output) * 0.85),
                    "phase": "analysis_outbox_continuation",
                },
            )
            composite_goal = self.automation.find_composite_goal(outbox.intent_task_id)
            if composite_goal is None:
                # Older standalone proposal outboxes predate CompositeGoal roots.
                # Keep their existing model path; only explicit durable roots
                # have a goal-level budget to share across stages.
                model_budget = None
            elif (
                composite_goal.conversation_id != outbox.conversation_id
                or composite_goal.project_id != outbox.project_id
            ):
                return commit_terminal(
                    expected_version=conversation.version,
                    status=AnalysisOutboxStatus.BLOCKED,
                    content="分析续接的复合目标身份与会话或项目不一致，系统已停止生成。",
                    payload=outbox_payload(
                        AnalysisOutboxStatus.BLOCKED,
                        "composite_goal_model_budget_scope_mismatch",
                    ),
                    error_code="composite_goal_model_budget_scope_mismatch",
                )
            else:
                try:
                    self.automation.get_composite_goal_model_budget(outbox.intent_task_id)
                except NotFoundError:
                    return commit_terminal(
                        expected_version=conversation.version,
                        status=AnalysisOutboxStatus.BLOCKED,
                        content="复合目标缺少持久模型预算记录，自动续接已失败关闭。",
                        payload=outbox_payload(
                            AnalysisOutboxStatus.BLOCKED,
                            "composite_goal_model_budget_missing",
                        ),
                        error_code="composite_goal_model_budget_missing",
                    )
                model_budget = _CompositeGoalModelBudgetSession(
                    self.automation,
                    intent_task_id=outbox.intent_task_id,
                    scope=(
                        f"analysis:{outbox.intent_task_id}:g{outbox.retry_generation}"
                        f":f{claim.fencing_token}"
                    ),
                )
            action_result = await self._await_fenced_generation(
                self.work_dialogue.prepare_analysis_continuation(
                    conversation,
                    packet,
                    source,
                    has_profiles=True,
                    persistent_model_budget=model_budget,
                ),
                claim_lost,
            )
            if action_result is None or claim_lost.is_set():
                return "claim_lost"

            model_error = action_result.payload.get("model_error")
            if isinstance(model_error, dict):
                error_code = model_error.get("code")
                await stop_heartbeat()
                if error_code == "composite_goal_model_budget_exhausted":
                    return commit_terminal(
                        expected_version=conversation.version,
                        status=AnalysisOutboxStatus.WAITING_USER,
                        content="复合目标的累计模型预算已用完，分析提案自动续接已暂停；不会通过重试重置预算。",
                        payload=outbox_payload(
                            AnalysisOutboxStatus.WAITING_USER,
                            "composite_goal_model_budget_exhausted",
                            retry_available=False,
                        ),
                        error_code="composite_goal_model_budget_exhausted",
                    )
                if error_code in {
                    "composite_goal_model_call_duplicate",
                    "composite_goal_model_reservation_invalid",
                }:
                    return commit_terminal(
                        expected_version=conversation.version,
                        status=AnalysisOutboxStatus.BLOCKED,
                        content=(
                            "模型调用预算 reservation 身份异常，系统拒绝重复分发并停止自动续接。"
                        ),
                        payload=outbox_payload(
                            AnalysisOutboxStatus.BLOCKED,
                            str(error_code),
                        ),
                        error_code=str(error_code),
                    )
                if outbox.attempt_count + 1 >= MAX_ANALYSIS_OUTBOX_ATTEMPTS:
                    return bounded_waiting_user(
                        expected_version=conversation.version,
                        error_code="analysis_model_retry_exhausted",
                        content="模型连续多次未能生成分析草稿，自动重试已暂停。请检查模型配置与服务状态后，点击本消息中的“重新尝试准备草稿”；系统不会自动决定统计方案或启动运行。",
                    )
                return defer(
                    error_code if isinstance(error_code, str) else "analysis_model_unavailable"
                )

            # Re-read the authority after generation. If evidence changed while the
            # model was working, discard its draft and persist only a blocked notice.
            current_source = self.automation.verify_analysis_outbox_source(outbox)
            current_proof = prepare_analysis_handoff(
                current_source.run_id, current_source.project_id, verify_files=True
            )
            if (
                current_source.source_evidence_hash != source.source_evidence_hash
                or current_proof.status != "ready"
                or current_proof.evidence_hash != proof.evidence_hash
            ):
                return commit_terminal(
                    expected_version=conversation.version,
                    status=AnalysisOutboxStatus.BLOCKED,
                    content=(
                        "分析草稿生成期间，预处理来源证据发生变化。"
                        "模型结果已丢弃，请重新检查 QC 和指标产物。"
                    ),
                    payload=outbox_payload(
                        AnalysisOutboxStatus.BLOCKED,
                        "analysis_outbox_source_changed_during_generation",
                    ),
                    error_code="analysis_outbox_source_changed_during_generation",
                )

            payload = dict(action_result.payload)
            proposal = payload.get("statistical_design_proposal")
            analysis_draft = payload.get("analysis_draft")
            if isinstance(proposal, dict):
                if not isinstance(analysis_draft, dict) or not isinstance(
                    analysis_draft.get("draft"), dict
                ):
                    return commit_terminal(
                        expected_version=conversation.version,
                        status=AnalysisOutboxStatus.BLOCKED,
                        content="统计提案未能绑定到完整的分析草稿，系统已停止自动续接。请重新提交分析请求。",
                        payload=outbox_payload(
                            AnalysisOutboxStatus.BLOCKED,
                            "analysis_outbox_proposal_contract_invalid",
                        ),
                        error_code="analysis_outbox_proposal_contract_invalid",
                    )
                status = AnalysisOutboxStatus.PROPOSAL_READY
                content = action_result.assistant_content
            else:
                status = AnalysisOutboxStatus.WAITING_USER
                issue_text: list[str] = []
                for key in ("draft_issues", "statistical_design_issues"):
                    values = payload.get(key)
                    if isinstance(values, list):
                        issue_text.extend(
                            item for item in values if isinstance(item, str) and item.strip()
                        )
                if isinstance(analysis_draft, dict):
                    values = analysis_draft.get("issues")
                    if isinstance(values, list):
                        issue_text.extend(
                            item for item in values if isinstance(item, str) and item.strip()
                        )
                questions = list(dict.fromkeys(issue_text))[:12]
                content = (
                    "分析草稿尚未完整，请补充或确认以下科研选择：\n"
                    + "\n".join(f"- {item}" for item in questions)
                    if questions
                    else (
                        "已收到原始分析请求，但本轮没有形成可审查的统计设计草稿。"
                        "请补充统计检验类型、指标类型、分组或条件信息，以及你希望使用的校正方法；"
                        "这些科研选择需要由你确认。"
                    )
                )
                payload["continuation_waiting_user"] = True

            terminal_error_code = None
            record_attempt = False
            if status is AnalysisOutboxStatus.WAITING_USER:
                terminal_error_code = "analysis_draft_incomplete"
                payload["analysis_outbox_result"] = {
                    "status": status.value,
                    "error_code": terminal_error_code,
                    "retry_available": False,
                }
            else:
                payload["analysis_outbox_result"] = {"status": status.value}
            await stop_heartbeat()
            if claim_lost.is_set():
                return "claim_lost"
            return commit_terminal(
                expected_version=conversation.version,
                status=status,
                content=content,
                payload=payload,
                user_content=source.analysis_request,
                error_code=terminal_error_code,
                record_attempt=record_attempt,
            )
        except ApplicationError as exc:
            await stop_heartbeat()
            if claim_lost.is_set():
                return "claim_lost"
            if exc.code == "analysis_outbox_claim_lost":
                # The terminal transaction rolls back its assistant message and
                # WorkTask binding together with this failed fenced update.
                return "claim_lost"
            if exc.code == "analysis_outbox_authority_indeterminate":
                if outbox.attempt_count + 1 >= MAX_ANALYSIS_OUTBOX_ATTEMPTS:
                    latest = self.repository.get_conversation(outbox.conversation_id)
                    return bounded_waiting_user(
                        expected_version=latest.version,
                        error_code="analysis_outbox_retry_exhausted",
                        content="预处理证据连续多次无法稳定读取，自动续接已暂停。确认数据库与运行证据恢复可读后，可手动重试；系统不会替你创建统计方案或运行。",
                    )
                return defer(exc.code)
            latest = self.repository.get_conversation(outbox.conversation_id)
            mismatch = (
                latest.mode is not ConversationMode.WORK or latest.project_id != outbox.project_id
            )
            return commit_terminal(
                expected_version=latest.version,
                status=AnalysisOutboxStatus.BLOCKED,
                content=(
                    (
                        "当前会话的工作区已切换，原自动续接已停止。"
                        "请回到原项目，在数据分析 Agent 中重新发起请求。"
                    )
                    if mismatch
                    else (
                        "预处理来源证据已发生冲突，系统已停止自动分析。"
                        "请检查冻结方案、QC 决定和指标产物后，再由你决定如何继续。"
                    )
                ),
                payload=(
                    {
                        "agent_role": WorkAgentRole.ANALYSIS.value,
                        "analysis_outbox_result": {
                            "status": AnalysisOutboxStatus.BLOCKED.value,
                            "error_code": "analysis_outbox_conversation_mismatch",
                            "retry_available": False,
                        },
                    }
                    if mismatch
                    else outbox_payload(AnalysisOutboxStatus.BLOCKED, exc.code)
                ),
                error_code=("analysis_outbox_conversation_mismatch" if mismatch else exc.code),
                safe_notice=mismatch,
            )
        except Exception as exc:
            await stop_heartbeat()
            if claim_lost.is_set():
                return "claim_lost"
            code = getattr(exc, "code", "analysis_outbox_consumer_error")
            if not isinstance(code, str):
                code = "analysis_outbox_consumer_error"
            logger.warning("Analysis outbox attempt deferred (%s)", code)
            if outbox.attempt_count + 1 >= MAX_ANALYSIS_OUTBOX_ATTEMPTS:
                try:
                    latest = self.repository.get_conversation(outbox.conversation_id)
                except Exception:
                    return "claim_lost"
                return bounded_waiting_user(
                    expected_version=latest.version,
                    error_code="analysis_outbox_retry_exhausted",
                    content="分析续接遇到连续暂态错误，自动重试已暂停。请检查服务状态后手动重试；系统不会自动决定统计方案或启动运行。",
                )
            return defer(code)
        finally:
            await stop_heartbeat()

    async def consume_next_result_interpretation_outbox(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
    ) -> str | None:
        """Generate one interpretation draft from a freshly verified real result."""
        claim = self.automation.claim_next_result_interpretation_outbox(
            worker_id, lease_seconds=lease_seconds
        )
        if claim is None:
            return None
        outbox = self.automation.get_result_interpretation_outbox(claim.task_id)
        stop_renewal = asyncio.Event()
        claim_lost = asyncio.Event()

        async def renew_claim() -> None:
            interval = max(1.0, lease_seconds / 3)
            while not stop_renewal.is_set():
                await asyncio.sleep(interval)
                if stop_renewal.is_set():
                    return
                try:
                    renewed = self.automation.renew_result_interpretation_claim(
                        claim, lease_seconds=lease_seconds
                    )
                except Exception:
                    logger.exception("Statistical result interpretation lease renewal failed")
                    claim_lost.set()
                    return
                if renewed is None:
                    claim_lost.set()
                    return

        heartbeat = asyncio.create_task(renew_claim())

        async def stop_heartbeat() -> None:
            stop_renewal.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

        def defer(error_code: str) -> str:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code):
                error_code = "result_interpretation_consumer_error"
            if outbox.attempt_count + 1 >= MAX_RESULT_INTERPRETATION_ATTEMPTS:
                try:
                    latest = self.repository.get_conversation(outbox.conversation_id)
                except Exception:
                    latest = None
                if latest is not None:
                    source_matches = (
                        latest.mode is ConversationMode.WORK
                        and latest.project_id == outbox.project_id
                    )
                    try:
                        if source_matches:
                            return commit_terminal(
                                expected_version=latest.version,
                                status=ResultInterpretationStatus.WAITING_USER,
                                content=(
                                    "统计结果解释连续多次遇到暂态错误，自动重试已暂停。"
                                    "确认服务与结果证据恢复后，可手动重新生成；"
                                    "系统不会更改方案或重新运行分析。"
                                ),
                                error_code="result_interpretation_retry_exhausted",
                                retry_available=True,
                            )
                        return commit_terminal(
                            expected_version=latest.version,
                            status=ResultInterpretationStatus.BLOCKED,
                            content=(
                                "当前会话已切换工作区或模式，原统计结果解释续接已停止。"
                                "请回到原项目查看已登记结果。"
                            ),
                            error_code="result_interpretation_conversation_mismatch",
                            safe_notice=True,
                            include_source=False,
                        )
                    except ConflictError:
                        logger.info(
                            "Statistical result interpretation terminal retry notice raced "
                            "with a conversation update"
                        )
            delay = min(300, 5 * 2 ** min(outbox.attempt_count, 6))
            committed = self.automation.defer_result_interpretation_claim(
                claim, retry_after_seconds=delay, error_code=error_code
            )
            return "deferred" if committed else "claim_lost"

        def status_payload(
            status: ResultInterpretationStatus,
            *,
            error_code: str | None = None,
            retry_available: bool = False,
            include_source: bool = True,
        ) -> dict[str, Any]:
            return {
                "agent_role": WorkAgentRole.ANALYSIS.value,
                "agent_name": "数据分析 Agent",
                "statistical_result_interpretation": {
                    **(
                        {
                            "task_id": outbox.task_id,
                            "retry_generation": outbox.retry_generation,
                            "result_id": outbox.result_id,
                            "result_version": outbox.result_version,
                        }
                        if include_source
                        else {}
                    ),
                    "status": status.value,
                    "retry_available": retry_available,
                    **({"error_code": error_code} if error_code else {}),
                },
            }

        def commit_terminal(
            *,
            expected_version: int,
            status: ResultInterpretationStatus,
            content: str,
            error_code: str | None = None,
            retry_available: bool = False,
            safe_notice: bool = False,
            include_source: bool = True,
            record_attempt: bool = False,
        ) -> str:
            continuation_key = (
                f"stat_result_interpretation:{outbox.task_id}:g{outbox.retry_generation}"
            )
            with self.automation.atomic():
                current = self.repository.get_conversation(outbox.conversation_id)
                if current.version != expected_version:
                    raise ConflictError(
                        "result_interpretation_conversation_changed",
                        "生成期间会话有新变化，本轮解释草稿没有提交。",
                    )
                source_matches = (
                    current.mode is ConversationMode.WORK
                    and current.project_id == outbox.project_id
                )
                if not source_matches and not safe_notice:
                    raise ConflictError(
                        "result_interpretation_conversation_mismatch",
                        "统计结果所属会话已切换模式或项目。",
                    )
                payload = status_payload(
                    status,
                    error_code=error_code,
                    retry_available=retry_available,
                    include_source=include_source and source_matches,
                )
                composite_goal = self.automation.find_composite_goal_for_analysis_task(
                    outbox.task_id
                )
                if composite_goal is not None and status is ResultInterpretationStatus.COMPLETED:
                    if (
                        composite_goal.analysis_work_task_id != outbox.task_id
                        or composite_goal.result_interpretation_outbox_task_id != outbox.task_id
                        or composite_goal.conversation_id != outbox.conversation_id
                        or composite_goal.project_id != outbox.project_id
                    ):
                        raise ConflictError(
                            "result_interpretation_composite_goal_mismatch",
                            "统计结果解释与复合目标的 active task 或结果绑定不一致。",
                        )
                    payload["analysis_intent_envelope"] = {
                        "status": "completed",
                        "task_id": composite_goal.intent_task_id,
                        "conversation_id": composite_goal.conversation_id,
                        "project_id": composite_goal.project_id,
                        "analysis_request_hash": composite_goal.analysis_request_hash,
                    }
                _, assistant = self.repository.append_assistant_continuation(
                    outbox.conversation_id,
                    continuation_key=continuation_key,
                    assistant_content=content,
                    assistant_payload=payload,
                    project_id=current.project_id,
                    work_task_id=None,
                    update_work_state=source_matches,
                )
                self.automation.complete_result_interpretation_outbox(
                    claim,
                    status=status,
                    continuation_message_id=assistant.message_id,
                    error_code=error_code,
                    record_attempt=record_attempt,
                )
            return status.value

        try:
            conversation = self.repository.get_conversation(outbox.conversation_id)
            if (
                conversation.mode is not ConversationMode.WORK
                or conversation.project_id != outbox.project_id
            ):
                await stop_heartbeat()
                return commit_terminal(
                    expected_version=conversation.version,
                    status=ResultInterpretationStatus.BLOCKED,
                    content="当前会话已切换工作区或模式，原统计结果解释续接已停止。请回到原项目查看已登记结果。",
                    error_code="result_interpretation_conversation_mismatch",
                    safe_notice=True,
                    include_source=False,
                )

            try:
                source = self.automation.verify_result_interpretation_source(outbox)
            except ApplicationError as exc:
                if exc.code == "result_interpretation_authority_indeterminate":
                    await stop_heartbeat()
                    if outbox.attempt_count + 1 >= MAX_RESULT_INTERPRETATION_ATTEMPTS:
                        return commit_terminal(
                            expected_version=conversation.version,
                            status=ResultInterpretationStatus.WAITING_USER,
                            content="统计结果证据连续多次无法稳定读取，自动解释已暂停。确认结果和数据库恢复可读后，可手动重试；系统不会更改统计方案或重新运行分析。",
                            error_code="result_interpretation_model_retry_exhausted",
                            retry_available=True,
                        )
                    return defer(exc.code)
                return commit_terminal(
                    expected_version=conversation.version,
                    status=ResultInterpretationStatus.BLOCKED,
                    content="统计结果或其冻结设计/产物证据未能重新核实，系统没有生成解释。请查看正式结果与运行记录。",
                    error_code=exc.code,
                )

            if not self.list_model_profiles():
                await stop_heartbeat()
                return commit_terminal(
                    expected_version=conversation.version,
                    status=ResultInterpretationStatus.WAITING_USER,
                    content="当前没有配置可用模型，统计结果解释报告草稿尚未生成。请先在设置中配置并测试模型，再点击本消息中的“重新生成结果解读草稿”。",
                    error_code="result_interpretation_model_profile_missing",
                    retry_available=True,
                )

            composite_goal = self.automation.find_composite_goal_for_analysis_task(outbox.task_id)
            model_budget = None
            if composite_goal is not None:
                if (
                    composite_goal.analysis_work_task_id != outbox.task_id
                    or composite_goal.result_interpretation_outbox_task_id != outbox.task_id
                    or composite_goal.conversation_id != outbox.conversation_id
                    or composite_goal.project_id != outbox.project_id
                ):
                    return commit_terminal(
                        expected_version=conversation.version,
                        status=ResultInterpretationStatus.BLOCKED,
                        content="统计结果解释与复合目标的当前分析任务或结果绑定不一致，系统已停止生成。",
                        error_code="result_interpretation_composite_goal_mismatch",
                    )
                try:
                    self.automation.get_composite_goal_model_budget(composite_goal.intent_task_id)
                except NotFoundError:
                    return commit_terminal(
                        expected_version=conversation.version,
                        status=ResultInterpretationStatus.BLOCKED,
                        content="复合目标缺少持久模型预算记录，统计结果解释已失败关闭。",
                        error_code="composite_goal_model_budget_missing",
                        retry_available=False,
                    )
                model_budget = _CompositeGoalModelBudgetSession(
                    self.automation,
                    intent_task_id=composite_goal.intent_task_id,
                    scope=(
                        f"result_interpretation:{outbox.task_id}"
                        f":g{outbox.retry_generation}:f{claim.fencing_token}"
                    ),
                )
            prepared = await self._await_fenced_generation(
                self.work_dialogue.prepare_statistical_result_interpretation(
                    conversation,
                    source,
                    preferred_profile_id=conversation.preferred_profile_id,
                    persistent_model_budget=model_budget,
                ),
                claim_lost,
            )
            if prepared is None or claim_lost.is_set():
                return "claim_lost"
            model_error = prepared.payload.get("model_error")
            if isinstance(model_error, dict):
                await stop_heartbeat()
                code = str(model_error.get("code", "result_interpretation_model_unavailable"))
                if code == "composite_goal_model_budget_exhausted":
                    return commit_terminal(
                        expected_version=conversation.version,
                        status=ResultInterpretationStatus.WAITING_USER,
                        content="复合目标的累计模型预算已用完，统计结果解释自动续接已暂停；不会通过重试重置预算。",
                        error_code=code,
                        retry_available=False,
                    )
                if code in {
                    "composite_goal_model_call_duplicate",
                    "composite_goal_model_reservation_invalid",
                }:
                    return commit_terminal(
                        expected_version=conversation.version,
                        status=ResultInterpretationStatus.BLOCKED,
                        content=(
                            "统计结果解释的模型调用 reservation 身份异常，"
                            "系统拒绝重复分发并已停止。"
                        ),
                        error_code=code,
                        retry_available=False,
                    )
                if outbox.attempt_count + 1 >= MAX_RESULT_INTERPRETATION_ATTEMPTS:
                    return commit_terminal(
                        expected_version=conversation.version,
                        status=ResultInterpretationStatus.WAITING_USER,
                        content="模型连续多次未能生成符合科研边界的解释草稿，自动重试已暂停。请检查模型配置与服务状态后，手动重试生成；系统不会更改方案或启动运行。",
                        error_code="result_interpretation_model_retry_exhausted",
                        retry_available=True,
                    )
                return defer(code)

            try:
                current_source = self.automation.verify_result_interpretation_source(outbox)
            except ApplicationError as exc:
                if exc.code == "result_interpretation_authority_indeterminate":
                    await stop_heartbeat()
                    return defer(exc.code)
                return commit_terminal(
                    expected_version=conversation.version,
                    status=ResultInterpretationStatus.BLOCKED,
                    content="生成期间统计结果或冻结证据发生变化，刚才的解释草稿已丢弃。请核对新的正式结果后重新查看。",
                    error_code="result_interpretation_source_changed_during_generation",
                )
            current_conversation = self.repository.get_conversation(outbox.conversation_id)
            if current_conversation.version != conversation.version:
                await stop_heartbeat()
                return defer("result_interpretation_conversation_changed")
            if current_source.source_evidence_hash != source.source_evidence_hash:
                return commit_terminal(
                    expected_version=conversation.version,
                    status=ResultInterpretationStatus.BLOCKED,
                    content=(
                        "生成期间统计结果、Plan 或 Artifact 证据发生变化，解释草稿已丢弃。"
                        "请以最新正式结果为准。"
                    ),
                    error_code="result_interpretation_source_changed_during_generation",
                )
            if claim_lost.is_set():
                return "claim_lost"
            return commit_terminal(
                expected_version=conversation.version,
                status=ResultInterpretationStatus.COMPLETED,
                content=prepared.assistant_content,
                record_attempt=True,
            )
        except ConflictError as exc:
            if exc.code in {
                "result_interpretation_claim_lost",
                "result_interpretation_conversation_changed",
            }:
                if exc.code == "result_interpretation_claim_lost":
                    return "claim_lost"
                return defer(exc.code)
            logger.exception("Statistical result interpretation outbox was rejected")
            return defer(exc.code)
        except Exception:
            logger.exception("Statistical result interpretation outbox consumer failed")
            return defer("result_interpretation_consumer_error")
        finally:
            await stop_heartbeat()

    def retry_result_interpretation_outbox(
        self,
        conversation_id: str,
        task_id: str,
        idempotency_key: str,
    ) -> ConversationView:
        request = ResultInterpretationRetryRequest(task_id=task_id)
        return self._idempotent(
            scope=f"conversations:{conversation_id}:result-interpretation-retry",
            key=idempotency_key,
            request=request,
            response_type=ConversationView,
            action=lambda: self._retry_result_interpretation_outbox(conversation_id, task_id),
        )

    def _retry_result_interpretation_outbox(
        self, conversation_id: str, task_id: str
    ) -> ConversationView:
        outbox = self.automation.get_result_interpretation_outbox(task_id)
        if outbox.conversation_id != conversation_id:
            raise ConflictError(
                "result_interpretation_retry_scope_mismatch",
                "该结果解释续接不属于当前会话。",
            )
        latest = self.automation.get_result_interpretation_outbox(task_id)
        if latest.status is ResultInterpretationStatus.READY and latest.retry_generation > 0:
            current = self.repository.get_conversation(conversation_id)
            return self._append_result_interpretation_retry(current, latest)
        if latest.status is not ResultInterpretationStatus.WAITING_USER:
            raise ConflictError(
                "result_interpretation_not_recoverable",
                "当前结果解释续接不处于可重试状态。",
            )
        requeued = self.automation.requeue_result_interpretation_outbox(
            task_id, recoverable_error_codes=RECOVERABLE_RESULT_INTERPRETATION_ERRORS
        )
        if requeued is None:
            raise ConflictError(
                "result_interpretation_not_recoverable",
                "该结果解释续接原因不支持自动重试。",
            )
        current = self.repository.get_conversation(conversation_id)
        return self._append_result_interpretation_retry(current, requeued)

    def _append_result_interpretation_retry(
        self,
        conversation: ConversationView,
        outbox: Any,
    ) -> ConversationView:
        with self.automation.atomic():
            current = self.repository.get_conversation(conversation.conversation_id)
            if current.version != conversation.version:
                raise ConflictError(
                    "revision_conflict", "会话已更新，请刷新后再试。", resource="conversation"
                )
            _, _assistant = self.repository.append_assistant_continuation(
                conversation.conversation_id,
                continuation_key=(
                    f"stat_result_interpretation_retry:{outbox.task_id}:g{outbox.retry_generation}"
                ),
                assistant_content="已排队重新生成这份统计结果解释草稿。系统会再次核验原结果证据，不会创建或启动新的统计运行。",
                assistant_payload={
                    "agent_role": WorkAgentRole.ANALYSIS.value,
                    "statistical_result_interpretation_retry": {
                        "task_id": outbox.task_id,
                        "retry_generation": outbox.retry_generation,
                        "status": ResultInterpretationStatus.READY.value,
                    },
                },
                project_id=current.project_id,
                work_task_id=None,
                update_work_state=True,
            )
            return self.repository.get_conversation(conversation.conversation_id)

    def retry_analysis_outbox(
        self, conversation_id: str, intent_task_id: str, idempotency_key: str
    ) -> ConversationView:
        """Manually requeue a recoverable WAITING_USER continuation."""
        request = AnalysisOutboxRetryRequest(intent_task_id=intent_task_id)
        return self._idempotent(
            scope=f"conversations:{conversation_id}:analysis-outbox-retry",
            key=idempotency_key,
            request=request,
            response_type=ConversationView,
            action=lambda: self._retry_analysis_outbox(conversation_id, request),
        )

    def _retry_analysis_outbox(
        self, conversation_id: str, request: AnalysisOutboxRetryRequest
    ) -> ConversationView:
        conversation = self.repository.get_conversation(conversation_id)
        outbox = self.automation.get_analysis_outbox(request.intent_task_id)
        if (
            outbox.conversation_id != conversation_id
            or conversation.mode is not ConversationMode.WORK
            or conversation.project_id != outbox.project_id
        ):
            raise ConflictError(
                "analysis_outbox_retry_authority_mismatch",
                "当前会话不再匹配该分析续接，无法重试。",
            )
        with self.automation.atomic():
            current = self.repository.get_conversation(conversation_id)
            latest = self.automation.get_analysis_outbox(request.intent_task_id)
            if (
                latest.conversation_id != conversation_id
                or current.mode is not ConversationMode.WORK
                or current.project_id != latest.project_id
            ):
                raise ConflictError(
                    "analysis_outbox_retry_authority_mismatch",
                    "当前会话不再匹配该分析续接，无法重试。",
                )
            if latest.status is AnalysisOutboxStatus.READY and latest.retry_generation > 0:
                return current
            if latest.status is not AnalysisOutboxStatus.WAITING_USER:
                raise ConflictError(
                    "analysis_outbox_not_recoverable",
                    "该分析续接当前没有可执行的人工重试。",
                )
            requeued = self.automation.requeue_analysis_outbox(
                request.intent_task_id,
                recoverable_error_codes=RECOVERABLE_ANALYSIS_OUTBOX_ERRORS,
            )
            if requeued is None:
                raise ConflictError(
                    "analysis_outbox_not_recoverable",
                    "该分析续接当前没有可执行的人工重试。",
                )
            self.repository.append_assistant_continuation(
                conversation_id,
                continuation_key=(
                    f"analysis_outbox_retry:{request.intent_task_id}:g{requeued.retry_generation}"
                ),
                assistant_content=(
                    "已按你的请求重新排入分析草稿准备。"
                    "系统会再次核实原始请求、预处理方案、QC 和指标产物；"
                    "这一步只准备统计提案，不会创建统计方案或启动运行。"
                ),
                assistant_payload={
                    "agent_role": WorkAgentRole.ANALYSIS.value,
                    "analysis_outbox_retry": {
                        "intent_task_id": request.intent_task_id,
                        "retry_generation": requeued.retry_generation,
                        "status": AnalysisOutboxStatus.READY.value,
                    },
                },
                project_id=outbox.project_id,
                work_task_id=None,
                update_work_state=False,
            )
            return self.repository.get_conversation(conversation_id)

    def _check_work_input_revision(
        self, conversation: ConversationView, saved: dict[str, Any] | None
    ) -> None:
        if not saved or not conversation.workspace_path:
            return
        if saved.get("input_evidence_stale") or saved.get("input_manifest_stale"):
            raise ConflictError("work_input_changed", "输入文件已变化，请通过对话重新读取并核实。")
        if not saved.get("input_revision") and not saved.get("facts"):
            return
        report = self.conversation_work.workspace_stage_candidates(conversation.workspace_path)
        revision = report.get("data_profile", {}).get("metadata_revision")
        if invalidate_input_evidence(saved, revision):
            raise ConflictError("work_input_changed", "输入文件已变化，请通过对话重新读取并核实。")

    answer_rsfmri_question: Callable[[RsFmriQuestionRequest], RsFmriAnswerView]
    create_run: Callable[[RunCreate, str], RunView]

    def create_conversation(
        self, request: ConversationCreate, idempotency_key: str
    ) -> ConversationView:
        title = request.title or (
            "fMRI 专项问答" if request.mode is ConversationMode.CHAT else "rs-fMRI 工作流"
        )
        welcome = (
            "这里是 fMRI 专项问答。我会检索项目内的 rs-fMRI、DPABI 和统计方法文档，"
            "并根据找到的证据回答。"
            if request.mode is ConversationMode.CHAT
            else "这里是 rs-fMRI 工作模式。请选择由你维护文件内容的工作区，"
            "我会协助准备预处理、查看进度和分析结果。"
        )
        return self._idempotent(
            scope=f"conversations:{request.mode.value}:create",
            key=idempotency_key,
            request=request,
            response_type=ConversationView,
            action=lambda: self.repository.create_conversation(
                mode=request.mode,
                title=title,
                welcome=welcome,
                workspace_path=request.workspace_path,
                preferred_profile_id=request.preferred_profile_id,
                project_id=request.project_id,
            ),
        )

    def list_conversations(self, mode: ConversationMode | None = None) -> list[ConversationView]:
        return self.repository.list_conversations(mode=mode)

    def get_conversation(self, conversation_id: str) -> ConversationView:
        return self.repository.get_conversation(conversation_id)

    def update_work_draft(
        self, conversation_id: str, request: WorkDraftUpdate, idempotency_key: str
    ) -> ConversationTurnView:
        def act() -> ConversationTurnView:
            conversation = self.repository.get_conversation(conversation_id)
            if conversation.mode is not ConversationMode.WORK:
                raise InputValidationError("work_draft_mode_required", "只能编辑 Work 方案草稿。")
            saved = latest_payload(conversation, "work_draft")
            if saved is None:
                raise ConflictError("work_draft_missing", "请先通过对话创建参数草稿。")
            if saved.get("consumed"):
                raise ConflictError(
                    "work_draft_already_started", "此方案已启动，请创建新方案后再编辑参数。"
                )
            if (
                not conversation.workspace_path
                or saved.get("workspace_path") != conversation.workspace_path
            ):
                raise ConflictError(
                    "work_draft_workspace_changed", "草稿与当前工作区不一致，请重新准备。"
                )
            draft = WorkDraft.model_validate(saved["draft"])
            self._check_work_input_revision(conversation, saved)
            if request.base_hash != draft_hash(draft):
                raise ConflictError("work_draft_changed", "参数草稿已更新，请核对最新版本后重试。")
            try:
                validate_draft_patch(request.patch)
                updated = merge_work_draft(draft, request.patch)
            except ValueError as exc:
                raise InputValidationError(
                    "work_draft_patch_invalid", "修改包含未知字段或不合法的参数结构。"
                ) from exc
            issues = review_issues(updated, saved.get("facts"))
            sources = dict(saved.get("sources", {}))

            def mark(value: Any, prefix: str = "") -> None:
                if isinstance(value, dict):
                    for name, child in value.items():
                        mark(child, f"{prefix}.{name}" if prefix else name)
                else:
                    sources[prefix] = "user"

            mark(request.patch)
            payload: dict[str, Any] = {
                "work_draft": draft_snapshot(
                    updated,
                    workspace_path=conversation.workspace_path,
                    sources=sources,
                    prior=saved,
                    facts=saved.get("facts"),
                ),
                "draft_issues": list(issues),
                "plan_preview": plan_preview(updated),
                "planning_decisions": planning_decisions(updated, sources),
            }
            if isinstance(saved.get("analysis_intent_task_id"), str):
                payload["work_draft"]["analysis_intent_task_id"] = saved["analysis_intent_task_id"]
                payload["work_draft"]["analysis_request_hash"] = saved.get("analysis_request_hash")
            review = render_draft_review(updated, sources)
            if not issues:
                payload["confirmation_request"] = {
                    "title": "确认方案参数",
                    "summary": review,
                    "options": [
                        {
                            "label": "确认并生成方案",
                            "description": "编译当前参数，不启动计算。",
                            "command": f"确认方案参数 {draft_hash(updated)[:12]}",
                        }
                    ],
                }
                dataset = latest_payload(conversation, "dataset")
                if dataset and conversation.project_id:
                    result = self.work_dialogue._compile(
                        conversation,
                        ConversationTurnCreate(content="准备更新后的方案审查"),
                        updated,
                        conversation.workspace_path,
                        conversation.project_id,
                        idempotency_key + ":review",
                        proposal=True,
                        snapshot=payload["work_draft"],
                    )
                    payload.update(result.payload)
                    review = result.assistant_content
            work_task_id = self._commit_preprocessing_work_task_binding(
                conversation=conversation,
                user_content=None,
                payload=payload,
                project_id=conversation.project_id,
            )
            stored, user, assistant, tool = self.repository.append_conversation_exchange(
                conversation_id,
                user_content="已在参数面板保存修改。",
                assistant_content=review,
                assistant_payload=payload,
                tool=None,
                workspace_path=conversation.workspace_path,
                preferred_profile_id=conversation.preferred_profile_id,
                project_id=conversation.project_id,
                active_run_id=conversation.active_run_id,
                work_task_id=work_task_id,
            )
            return ConversationTurnView(
                conversation=stored, user_message=user, assistant_message=assistant, tool_call=tool
            )

        return self._idempotent(
            scope=f"conversations:{conversation_id}:work-draft",
            key=idempotency_key,
            request=request,
            response_type=ConversationTurnView,
            action=act,
        )

    async def confirm_work_action(
        self,
        conversation_id: str,
        request: WorkConfirmationCreate,
        idempotency_key: str,
    ) -> ConversationTurnView:
        return await self.send_conversation_turn(
            conversation_id,
            ConversationTurnCreate(content=request.command),
            idempotency_key,
            _confirmation=request,
        )

    def _validate_work_confirmation(
        self, conversation: ConversationView, request: WorkConfirmationCreate
    ) -> None:
        # Validate only after the idempotency lookup: a durable response must
        # remain recoverable even when the conversation has since changed.
        if conversation.mode is not ConversationMode.WORK:
            raise InputValidationError(
                "work_confirmation_mode_required", "此操作仅用于 Work 会话。"
            )
        if conversation.foreground_agent_role is not WorkAgentRole.PREPROCESSING:
            raise ConflictError(
                "work_agent_confirmation_mismatch", "当前负责 Agent 已切换，请先切回预处理 Agent。"
            )
        saved = latest_payload(conversation, "work_draft")
        # Re-freezing is the recovery operation itself. The exact issued option
        # and the coordinator's live stage/path checks still apply below.
        if not re.fullmatch(STAGE_CONFIRMATION_PATTERN, request.command):
            self._check_work_input_revision(conversation, saved)
        if request.expected_draft_hash and (
            not saved or saved.get("hash") != request.expected_draft_hash
        ):
            raise ConflictError("work_draft_changed", "方案已更新，请审查最新版本。")
        confirmation = latest_payload(conversation, "confirmation_request")
        commands = [item.get("command") for item in (confirmation or {}).get("options", [])]
        if request.command not in commands:
            raise ConflictError("work_confirmation_changed", "确认操作已更新，请重新审查方案。")

    def prepare_work_review(
        self,
        conversation_id: str,
        request: WorkReviewCreate,
        idempotency_key: str,
    ) -> ConversationTurnView:
        def act() -> ConversationTurnView:
            conversation = self.repository.get_conversation(conversation_id)
            saved = latest_payload(conversation, "work_draft")
            if conversation.mode is not ConversationMode.WORK or not saved:
                raise ConflictError("work_draft_missing", "请先准备 Work 方案。")
            draft = WorkDraft.model_validate(saved["draft"])
            if (
                saved.get("workspace_path") != conversation.workspace_path
                or draft_hash(draft) != request.base_hash
            ):
                raise ConflictError("work_draft_changed", "工作区或方案已更新，请审查最新版本。")
            if saved.get("consumed"):
                raise ConflictError("work_review_already_started", "此方案已启动，请查看运行卡片。")
            recovery: WorkActionResult | None = None
            try:
                self._check_work_input_revision(conversation, saved)
            except ConflictError as exc:
                if exc.code != "work_input_changed":
                    raise
                report = self.conversation_work.workspace_stage_candidates(
                    conversation.workspace_path
                )
                revision = report.get("data_profile", {}).get("metadata_revision")
                invalidated = invalidate_input_evidence(saved, revision) or saved
                recovery = WorkActionResult(
                    "旧输入证据或确认已失效，参数选择已保留。请重新确认输入阶段并读取采集信息，再审查方案。",
                    payload={
                        "work_draft": invalidated,
                        "draft_issues": invalidated.get("review_issues", []),
                        "plan_preview": plan_preview(draft),
                        "planning_decisions": planning_decisions(
                            draft, invalidated.get("sources", {})
                        ),
                        "confirmation_request": self.work_dialogue.input_recovery_confirmation(
                            conversation.workspace_path, list(report.get("candidates", []))
                        ),
                    },
                )
            issues = review_issues(draft, saved.get("facts"))
            if issues and recovery is None:
                raise ConflictError("work_review_incomplete", "请先处理方案中的待填写或冲突项。")
            plan_data = latest_payload(conversation, "plan_revision")
            if recovery is not None:
                result = recovery
            elif saved.get("compiled") and plan_data and conversation.project_id:
                plan = self.get_plan(str(plan_data["plan_revision_id"]))
                qc = (
                    QcExecutionMode.REQUIRE
                    if draft.qc_mode == "head_motion"
                    else QcExecutionMode.SKIP
                    if draft.qc_mode == "skip"
                    else None
                )
                confirmation_request = self.work_dialogue.run_confirmation_request(
                    conversation_id=conversation_id,
                    project_id=conversation.project_id,
                    plan_revision_id=plan.plan_revision_id,
                    plan_hash=plan.plan_hash,
                    qc_mode=qc,
                    summary=execution_summary(draft),
                )
                result = WorkActionResult(
                    "方案保持原版本，启动确认已更新。请审查后确认本次计算。",
                    payload={
                        "work_draft": saved,
                        "draft_issues": [],
                        "plan_preview": plan_preview(draft),
                        "planning_decisions": planning_decisions(draft, saved.get("sources", {})),
                        "plan_revision": plan.model_dump(mode="json"),
                        "confirmation_request": confirmation_request,
                    },
                )
            else:
                result = self.work_dialogue._compile(
                    conversation,
                    ConversationTurnCreate(content="重新校验当前方案"),
                    draft,
                    conversation.workspace_path,
                    conversation.project_id,
                    idempotency_key,
                    proposal=True,
                    snapshot=saved,
                )
            work_task_id = self._commit_preprocessing_work_task_binding(
                conversation=conversation,
                user_content=None,
                payload=result.payload,
                project_id=result.project_id or conversation.project_id,
            )
            stored, user, assistant, tool = self.repository.append_conversation_exchange(
                conversation_id,
                user_content="重新校验当前方案与启动确认。",
                assistant_content=result.assistant_content,
                assistant_payload=result.payload,
                tool=result.tool,
                workspace_path=conversation.workspace_path,
                preferred_profile_id=conversation.preferred_profile_id,
                project_id=result.project_id or conversation.project_id,
                active_run_id=conversation.active_run_id,
                work_task_id=work_task_id,
            )
            return ConversationTurnView(
                conversation=stored, user_message=user, assistant_message=assistant, tool_call=tool
            )

        return self._idempotent(
            scope=f"conversations:{conversation_id}:work-review",
            key=idempotency_key,
            request=request,
            response_type=ConversationTurnView,
            action=act,
        )

    def get_conversation_context(self, conversation_id: str) -> ConversationContextView:
        return self.repository.get_conversation_context(conversation_id)

    def create_conversation_memory(
        self, conversation_id: str, request: MemoryCreate, idempotency_key: str
    ) -> MemoryView:
        def act() -> MemoryView:
            memory = self.repository.create_memory(conversation_id, request)
            self.event_store.append_event(
                project_id=memory.project_id,
                run_id=None,
                event_type="MemoryUpdated",
                severity="info",
                payload={"memory_id": memory.memory_id, "action": "create"},
            )
            return memory

        return self._idempotent(
            scope=f"conversations:{conversation_id}:memories:create",
            key=idempotency_key,
            request=request,
            response_type=MemoryView,
            action=act,
        )

    def update_conversation_memory(
        self,
        conversation_id: str,
        memory_id: str,
        request: MemoryUpdate,
        idempotency_key: str,
    ) -> MemoryView:
        def act() -> MemoryView:
            memory = self.repository.update_memory(conversation_id, memory_id, request)
            self.event_store.append_event(
                project_id=memory.project_id,
                run_id=None,
                event_type="MemoryUpdated",
                severity="info",
                payload={"memory_id": memory.memory_id, "action": request.action.value},
            )
            return memory

        return self._idempotent(
            scope=f"conversations:{conversation_id}:memories:{memory_id}",
            key=idempotency_key,
            request=request,
            response_type=MemoryView,
            action=act,
        )

    async def send_conversation_turn(
        self,
        conversation_id: str,
        request: ConversationTurnCreate,
        idempotency_key: str,
        *,
        _confirmation: WorkConfirmationCreate | None = None,
    ) -> ConversationTurnView:
        conversation = self.repository.get_conversation(conversation_id)
        idempotency_key_hash = content_hash(idempotency_key)
        selected_analysis_task = conversation_work_task(conversation, WorkAgentRole.ANALYSIS)
        initial_selected_intent_id = (
            selected_analysis_task.analysis_intent_task_id
            if selected_analysis_task is not None
            else None
        )
        durable_idempotency_key = (
            idempotency_key_hash
            if conversation.mode is ConversationMode.WORK
            else idempotency_key
        )
        if (
            request.analysis_intent_task_id is not None
            or request.external_analysis_import_id is not None
            or request.start_new_analysis_goal
        ) and conversation.mode is not ConversationMode.WORK:
            raise InputValidationError(
                "analysis_goal_work_conversation_required",
                "复合分析目标只能在 Work 会话中恢复或新建。",
            )
        if (
            request.preprocessing_task_id is not None
            and conversation.mode is not ConversationMode.WORK
        ):
            raise InputValidationError(
                "analysis_outbox_work_conversation_required",
                "指定预处理任务的分析目标只能登记在 Work 会话中。",
            )
        if conversation.mode is ConversationMode.CHAT and _confirmation is None:
            return await self._send_chat_turn(
                conversation_id,
                request,
                idempotency_key,
            )

        async def prepare() -> tuple[
            int,
            WorkActionResult,
            dict[str, Any],
            tuple[MemoryCandidateDraft, ...],
            str | None,
            list[str],
            str | None,
            dict[str, Any] | None,
            AnalysisOutboxRegistrationSource | None,
            CompositeGoalIdentity | None,
            str | None,
            str | None,
        ]:
            conversation = self.repository.get_conversation(conversation_id)
            if _confirmation is not None:
                self._validate_work_confirmation(conversation, _confirmation)
            workspace_path = request.workspace_path or conversation.workspace_path
            preferred_profile_id = (
                request.preferred_profile_id
                if request.preferred_profile_id is not None
                else conversation.preferred_profile_id
            )
            project_id = request.project_id or conversation.project_id
            analysis_goal = None
            analysis_original_request: str | None = None
            persistent_model_budget = None
            selected_analysis_task = conversation_work_task(conversation, WorkAgentRole.ANALYSIS)
            selected_intent_id = (
                selected_analysis_task.analysis_intent_task_id
                if selected_analysis_task is not None
                else None
            )
            if selected_intent_id != initial_selected_intent_id:
                raise ConflictError(
                    "analysis_goal_selection_conflict",
                    "请求准备期间 Analysis 角色槽已切换，请基于当前目标重试。",
                )
            requested_role = (
                WorkAgentRole.ANALYSIS
                if request.analysis_intent_task_id is not None
                or request.external_analysis_import_id is not None
                or request.start_new_analysis_goal
                else request.agent_role
            )
            selected_role = work_agent_role_for_turn(
                request.content,
                requested=requested_role,
                conversation=conversation,
            )
            if request.analysis_intent_task_id is not None and (
                request.analysis_intent_task_id != selected_intent_id
            ):
                raise ConflictError(
                    "analysis_goal_selection_conflict",
                    "请求中的分析目标 ID 与当前 Analysis 角色槽不一致。",
                )
            selected_goal = (
                self.automation.find_composite_goal(selected_intent_id)
                if selected_intent_id is not None
                else None
            )
            turn_selected_goal = (
                selected_goal if selected_role is WorkAgentRole.ANALYSIS else None
            )
            if request.start_new_analysis_goal and _confirmation is not None:
                raise InputValidationError(
                    "analysis_goal_confirmation_not_supported",
                    "工作确认请求不能创建新的独立分析目标。",
                )
            if selected_goal is not None and (
                selected_goal.conversation_id != conversation_id
                or selected_goal.project_id != project_id
            ):
                raise ConflictError(
                    "analysis_goal_selection_conflict", "当前 Analysis 角色槽的目标范围不匹配。"
                )
            if request.analysis_intent_task_id is not None and selected_goal is None:
                raise ConflictError(
                    "analysis_goal_selection_conflict", "当前 Analysis 角色槽没有可恢复的目标记录。"
                )

            def read_external_source(import_id: str) -> ExternalAnalysisImportView:
                if conversation.mode is not ConversationMode.WORK or project_id is None:
                    raise InputValidationError(
                        "analysis_only_work_project_required",
                        "独立统计分析需要绑定到一个 Work 会话和当前项目。",
                    )
                imported = self.get_external_import(import_id)
                if (
                    imported.project_id != project_id
                    or imported.status != "included"
                    or imported.selection is None
                    or imported.inclusion_hash is None
                    or imported.inspection.metric_kind is None
                    or imported.inspection.scaling is None
                ):
                    raise InputValidationError(
                        "analysis_only_external_source_unverified",
                        "请从当前项目选择已纳入并登记掩膜的外部分析来源。",
                    )
                return imported

            if request.start_new_analysis_goal:
                assert request.external_analysis_import_id is not None
                imported = read_external_source(request.external_analysis_import_id)
                analysis_goal = self.automation.ensure_analysis_only_external_goal(
                    conversation_id=conversation_id,
                    project_id=str(project_id),
                    analysis_request=request.content,
                    imported=imported,
                    idempotency_key_hash=idempotency_key_hash,
                )
                analysis_original_request = request.content.strip()
            elif request.external_analysis_import_id is not None:
                if turn_selected_goal is not None:
                    if turn_selected_goal.goal_kind != "analysis_only_external_import":
                        raise ConflictError(
                            "analysis_goal_selection_conflict",
                            "当前选择的是其他分析目标；如需独立外部指标目标，请明确新建目标。",
                        )
                    if (
                        turn_selected_goal.source_external_import_id
                        != request.external_analysis_import_id
                    ):
                        raise ConflictError(
                            "analysis_goal_selection_conflict",
                            "所选外部来源与当前 Analysis 目标不一致。",
                        )
                    analysis_goal = turn_selected_goal
                else:
                    imported = read_external_source(request.external_analysis_import_id)
                    analysis_goal = self.automation.ensure_analysis_only_external_goal(
                        conversation_id=conversation_id,
                        project_id=str(project_id),
                        analysis_request=request.content,
                        imported=imported,
                        idempotency_key_hash=idempotency_key_hash,
                    )
                    analysis_original_request = request.content.strip()
            elif turn_selected_goal is not None and (
                turn_selected_goal.goal_kind == "analysis_only_external_import"
            ):
                analysis_goal = turn_selected_goal

            if analysis_goal is not None:
                imported = read_external_source(
                    analysis_goal.source_external_import_id or ""
                )
                _verify_analysis_only_external_source(analysis_goal, imported)
                analysis_original_request = _analysis_goal_original_request(
                    conversation,
                    analysis_goal,
                    current_request=request.content.strip(),
                )
                persistent_model_budget = _CompositeGoalModelBudgetSession(
                    self.automation,
                    intent_task_id=analysis_goal.intent_task_id,
                    scope=f"analysis-only:{analysis_goal.intent_task_id}:{idempotency_key_hash}",
                )
            elif turn_selected_goal is not None:
                persistent_model_budget = _CompositeGoalModelBudgetSession(
                    self.automation,
                    intent_task_id=turn_selected_goal.intent_task_id,
                    scope=f"composite-goal:{turn_selected_goal.intent_task_id}:{idempotency_key_hash}",
                )
            if request.preprocessing_task_id is not None:
                if _confirmation is not None:
                    raise InputValidationError(
                        "analysis_outbox_confirmation_not_supported",
                        "工作确认请求不能登记新的分析目标。",
                    )
                if project_id is None or request.target_run_id is None:
                    raise InputValidationError(
                        "analysis_outbox_source_required",
                        "登记分析目标必须提供当前项目、预处理任务和明确选中的 Run。",
                    )
                source = self.automation.inspect_analysis_outbox_registration_source(
                    conversation_id=conversation_id,
                    project_id=project_id,
                    preprocessing_task_id=request.preprocessing_task_id,
                    run_id=request.target_run_id,
                )
                analysis_request = request.content.strip()
                analysis_request_hash = content_hash(analysis_request)
                intent_id = handoff_identity(
                    {
                        "task_kind": "preprocess_then_analyze",
                        "conversation_id": conversation_id,
                        "project_id": project_id,
                        "preprocessing_task_id": source.preprocessing_task_id,
                        "source_run_id": source.run_id,
                        "analysis_request_hash": analysis_request_hash,
                        "idempotency_key": idempotency_key,
                    }
                )[:24]
                references = {
                    "project_id": project_id,
                    "source_preprocessing_task_id": source.preprocessing_task_id,
                    "source_run_id": source.run_id,
                    "source_plan_revision_id": source.plan_revision_id,
                    "source_plan_hash": source.plan_hash,
                    "input_manifest_hash": source.manifest_hash,
                }
                intent = {
                    "task_kind": "preprocess_then_analyze",
                    "task_id": intent_id,
                    "conversation_id": conversation_id,
                    "project_id": project_id,
                    "analysis_request": analysis_request,
                    "status": (
                        "analysis_ready"
                        if source.ready_artifact_ids
                        else "waiting_for_preprocessing"
                    ),
                    "data_references": references,
                    "evidence_hash": handoff_identity(
                        {
                            "intent_task_id": intent_id,
                            "data_references": references,
                            "task_version": source.task_version,
                            "step_version": source.step_version,
                            "run_version": source.run_version,
                            "plan_version": source.plan_version,
                            "verified_artifact_ids": source.ready_artifact_ids,
                        }
                    ),
                }
                action_result = WorkActionResult(
                    assistant_content=(
                        "已将这项分析请求绑定到你选中的预处理运行。"
                        "来源完成并通过核验后，系统会准备可审查的统计方案；不会自动启动统计运行。"
                    ),
                    payload={
                        "agent_role": WorkAgentRole.ANALYSIS.value,
                        "agent_name": "数据分析 Agent",
                        "analysis_dependency": intent,
                        "analysis_intent_envelope": {
                            "status": "active",
                            "task_id": intent_id,
                            "conversation_id": conversation_id,
                            "project_id": project_id,
                            "analysis_request_hash": analysis_request_hash,
                        },
                        "analysis_intent_registration": {
                            "status": "ready" if source.ready_artifact_ids else "pending",
                            "source_run_id": source.run_id,
                        },
                    },
                    workspace_path=workspace_path,
                    project_id=project_id,
                    active_run_id=source.run_id,
                )
                return (
                    conversation.version,
                    action_result,
                    {},
                    (),
                    None,
                    [],
                    preferred_profile_id,
                    None,
                    source,
                    None,
                    None,
                    selected_intent_id,
                )
            turn_goal = analysis_goal or turn_selected_goal
            dialogue_request = request
            if turn_goal is not None:
                dialogue_request = request.model_copy(
                    update={
                        "analysis_intent_task_id": turn_goal.intent_task_id,
                        "agent_role": WorkAgentRole.ANALYSIS,
                        "external_analysis_import_id": (
                            analysis_goal.source_external_import_id
                            if analysis_goal is not None
                            and request.action is ConversationAction.AUTO
                            else request.external_analysis_import_id
                        ),
                        "start_new_analysis_goal": False,
                    }
                )
            active_run_id = request.target_run_id or run_for_agent_context(
                conversation, selected_role
            )
            frozen_analysis = (
                self.resolve_selected_frozen_analysis(conversation)
                if selected_role is WorkAgentRole.ANALYSIS
                and (
                    request.external_analysis_import_id is None
                    or (
                        turn_goal is not None
                        and selected_intent_id == turn_goal.intent_task_id
                    )
                )
                else None
            )
            selected_plan_id = request.plan_revision_id
            if frozen_analysis is not None:
                frozen_role, frozen_plan = frozen_analysis
                if (
                    project_id != frozen_plan.project_id
                    or request.target_run_id not in {None, frozen_role.run_id}
                    or request.plan_revision_id not in {None, frozen_role.plan_revision_id}
                ):
                    raise ConflictError(
                        "analysis_goal_selection_conflict", "请求来源与当前选中的统计目标不一致。"
                    )
                active_run_id = frozen_role.run_id
                selected_plan_id = frozen_role.plan_revision_id
                dialogue_request = dialogue_request.model_copy(
                    update={"external_analysis_import_id": None}
                )
            if analysis_goal is not None and selected_intent_id != analysis_goal.intent_task_id:
                # A newly-created goal starts with its own source context and
                # cannot inherit another goal's selected Run.
                active_run_id = None
                selected_plan_id = None
            # This turn's context must use the selected role's run, while the
            # persisted conversation retains both roles' independent references.
            if active_run_id is not None:
                target_run = self.get_run(active_run_id)
                if project_id is None or target_run.project_id != project_id:
                    if request.target_run_id is not None:
                        raise InputValidationError(
                            "work_target_run_project_mismatch",
                            "所选运行不属于当前 Work 项目，无法将它绑定到本轮分析。",
                            run_id=request.target_run_id,
                            project_id=project_id,
                        )
                    active_run_id = None
            conversation = conversation.model_copy(update={"active_run_id": active_run_id})
            confirmed_stage = None
            for message in reversed(conversation.messages):
                manifest_payload = message.payload.get("manifest")
                if isinstance(manifest_payload, dict) and manifest_payload.get(
                    "selected_input_stage"
                ):
                    confirmed_stage = str(manifest_payload["selected_input_stage"])
                    break
            workspace_stage_candidates = self.conversation_work.workspace_stage_candidates(
                workspace_path,
                confirmed_stage=confirmed_stage,
            )
            try:
                prepared_context = self.conversation_context.prepare_work(
                    conversation,
                    question=request.content,
                    preferred_profile_id=preferred_profile_id,
                    project_id=project_id,
                    active_run_id=active_run_id,
                    plan_revision_id=selected_plan_id,
                    workspace_stage_candidates=workspace_stage_candidates,
                )
                work_packet = prepared_context.packet
                context_state = prepared_context.state
                preferred_profile_id = prepared_context.profile_id
            except ContextBudgetError as exc:
                raise InputValidationError(
                    "context_input_too_large", "当前消息超过所选模型的上下文预算。"
                ) from exc

            if request.action is ConversationAction.AUTO or frozen_analysis is not None:
                action_result = await self.work_dialogue.respond(
                    conversation,
                    dialogue_request,
                    work_packet,
                    workspace_path=workspace_path,
                    project_id=project_id,
                    preferred_profile_id=preferred_profile_id,
                    workspace_content_consent=(
                        self.workspace_content_consent_granted(
                            conversation_id, workspace_path, preferred_profile_id, request.model
                        )
                        if hasattr(self, "workspace_content_consent_granted")
                        else False
                    ),
                    idempotency_key=durable_idempotency_key,
                    has_profiles=bool(self.list_model_profiles()),
                    persistent_model_budget=persistent_model_budget,
                )
            else:
                action_result = self.conversation_work.execute(
                    request,
                    workspace_path=workspace_path,
                    active_run_id=active_run_id,
                    project_id=project_id,
                    idempotency_key=durable_idempotency_key,
                    conversation_id=conversation_id,
                )
                role = request.agent_role or (
                    WorkAgentRole.ANALYSIS
                    if request.action is ConversationAction.GET_STATISTICAL_RESULTS
                    else WorkAgentRole.PREPROCESSING
                )
                action_result = replace(
                    action_result,
                    payload={
                        "agent_role": role.value,
                        "agent_name": (
                            "数据分析 Agent" if role is WorkAgentRole.ANALYSIS else "预处理 Agent"
                        ),
                        **action_result.payload,
                    },
                )
            if (
                analysis_goal is not None
                and action_result.payload.get("agent_role") == WorkAgentRole.ANALYSIS.value
            ):
                payload = dict(action_result.payload)
                request_hash = analysis_goal.analysis_request_hash
                payload["analysis_dependency"] = {
                    "task_kind": analysis_goal.goal_kind,
                    "task_id": analysis_goal.intent_task_id,
                    "conversation_id": conversation_id,
                    "project_id": analysis_goal.project_id,
                    "analysis_request": analysis_original_request,
                    "status": "analysis_started",
                    "data_references": {
                        "external_import_id": analysis_goal.source_external_import_id,
                        "external_import_version": analysis_goal.source_import_version,
                        "source_tree_hash": analysis_goal.source_tree_hash,
                        "inclusion_hash": analysis_goal.source_inclusion_hash,
                        "input_manifest_hash": analysis_goal.source_manifest_hash,
                    },
                    "evidence_hash": analysis_goal.source_revision_hash,
                }
                payload["analysis_intent_envelope"] = {
                    "status": "active",
                    "task_id": analysis_goal.intent_task_id,
                    "conversation_id": conversation_id,
                    "project_id": analysis_goal.project_id,
                    "analysis_request_hash": request_hash,
                }
                analysis_draft = payload.get("analysis_draft")
                if isinstance(analysis_draft, dict):
                    payload["analysis_draft"] = {
                        **analysis_draft,
                        "analysis_intent_task_id": analysis_goal.intent_task_id,
                        "analysis_request_hash": request_hash,
                    }
                payload["analysis_target"] = {
                    "goal_kind": analysis_goal.goal_kind,
                    "intent_task_id": analysis_goal.intent_task_id,
                    "source_external_import_id": analysis_goal.source_external_import_id,
                    "source_revision_hash": analysis_goal.source_revision_hash,
                    "input_manifest_hash": analysis_goal.source_manifest_hash,
                }
                action_result = replace(action_result, payload=payload)
            model_metadata = action_result.payload.get("model")
            return (
                conversation.version,
                action_result,
                work_packet.metadata,
                prepared_context.memory_candidates,
                context_state.summary.summary_id if context_state.summary else None,
                [item.memory_id for item in context_state.memories if item.pinned],
                preferred_profile_id,
                model_metadata,
                None,
                analysis_goal,
                analysis_original_request,
                selected_intent_id,
            )

        def finalize(
            prepared: tuple[
                int,
                WorkActionResult,
                dict[str, Any],
                tuple[MemoryCandidateDraft, ...],
                str | None,
                list[str],
                str | None,
                dict[str, Any] | None,
                AnalysisOutboxRegistrationSource | None,
                CompositeGoalIdentity | None,
                str | None,
                str | None,
            ],
        ) -> ConversationTurnView:
            (
                expected_conversation_version,
                action_result,
                context_metadata,
                memory_candidates,
                summary_id,
                pinned_memory_ids,
                preferred_profile_id,
                model_metadata,
                registration_source,
                analysis_goal,
                _analysis_original_request,
                selected_intent_id,
            ) = prepared
            conversation = self.repository.get_conversation(conversation_id)
            if conversation.version != expected_conversation_version:
                raise ConflictError(
                    "conversation_changed_during_prepare",
                    "工作请求准备期间对话已变化，请基于最新状态重试。",
                )
            current_selected = conversation_work_task(conversation, WorkAgentRole.ANALYSIS)
            current_selected_id = (
                current_selected.analysis_intent_task_id if current_selected is not None else None
            )
            if current_selected_id != selected_intent_id:
                raise ConflictError(
                    "analysis_goal_selection_conflict",
                    "模型准备期间 Analysis 角色槽已切换，迟到结果未写入。",
                )
            if analysis_goal is not None:
                try:
                    current_goal = self.automation.get_composite_goal(
                        conversation_id, analysis_goal.intent_task_id
                    )
                    imported = self.get_external_import(
                        analysis_goal.source_external_import_id or ""
                    )
                except ApplicationError as exc:
                    raise ConflictError(
                        "analysis_goal_external_source_changed",
                        "模型准备期间外部来源或目标记录已无法核验，迟到结果未写入。",
                    ) from exc
                if current_goal != analysis_goal:
                    raise ConflictError(
                        "analysis_goal_selection_conflict",
                        "模型准备期间分析目标身份已变化，迟到结果未写入。",
                    )
                _verify_analysis_only_external_source(analysis_goal, imported)
            if action_result.commit is not None:
                proposed = action_result
                committed = action_result.commit()
                action_result = replace(
                    committed,
                    payload={**proposed.payload, **committed.payload},
                )
            project_id = action_result.project_id or request.project_id or conversation.project_id
            workspace_path = action_result.workspace_path
            if conversation.mode is ConversationMode.WORK and workspace_path is None:
                workspace_path = request.workspace_path or conversation.workspace_path
            active_run_id = action_result.active_run_id
            payload = action_result.payload
            work_task_id: str | None = None
            if (
                conversation.mode is ConversationMode.WORK
                and payload.get("agent_role", conversation.foreground_agent_role.value)
                == WorkAgentRole.PREPROCESSING.value
            ):
                work_task_id = self._commit_preprocessing_work_task_binding(
                    conversation=conversation,
                    user_content=request.content,
                    payload=payload,
                    project_id=project_id,
                    bind_run=(
                        action_result.tool is not None
                        and action_result.tool.get("tool_name") == "start_dpabi_preprocessing"
                    ),
                )
            elif (
                conversation.mode is ConversationMode.WORK
                and registration_source is None
                and payload.get("agent_role", conversation.foreground_agent_role.value)
                == WorkAgentRole.ANALYSIS.value
            ):
                work_task_id = self._commit_analysis_work_task_binding(
                    conversation=conversation,
                    user_content=request.content,
                    payload=payload,
                    project_id=project_id,
                )
            if action_result.tool is not None:
                action_result = replace(
                    action_result,
                    tool={
                        **action_result.tool,
                        "agent_role": payload.get("agent_role"),
                    },
                )
            payload["context"] = context_metadata
            timings.add("prepare_total", timings.clock() - request_started)
            payload["work_timings"] = {
                "stages": timings.snapshot(),
                "note": "各阶段可能包含子步骤，耗时不可直接相加；未出现的阶段本轮未测量。",
            }

            stored, user_message, assistant_message, stored_tool = (
                self.repository.append_conversation_exchange(
                    conversation_id,
                    user_content=request.content,
                    assistant_content=action_result.assistant_content,
                    assistant_payload=payload,
                    tool=action_result.tool,
                    tool_calls=_loop_tool_calls(payload),
                    workspace_path=workspace_path,
                    preferred_profile_id=preferred_profile_id,
                    project_id=project_id,
                    active_run_id=active_run_id,
                    work_task_id=work_task_id,
                )
            )
            if registration_source is not None:
                intent = payload.get("analysis_dependency")
                if not isinstance(intent, dict) or project_id is None:
                    raise ConflictError(
                        "analysis_outbox_intent_missing",
                        "显式分析目标没有持久化其原始意图。",
                    )
                self.automation.register_analysis_proposal_outbox(
                    intent=intent,
                    conversation_id=conversation_id,
                    project_id=project_id,
                    source=registration_source,
                )
            candidate_ids = self.conversation_context.persist_memory_candidates(
                conversation_id,
                memory_candidates,
                user_message.message_id,
            )
            if candidate_ids:
                self.event_store.append_event(
                    project_id=project_id,
                    run_id=active_run_id,
                    event_type="MemoryCandidatesUpdated",
                    severity="info",
                    payload={"conversation_id": conversation_id, "memory_ids": candidate_ids},
                )
            self.conversation_context.persist_snapshot(
                conversation_id,
                assistant_message_id=assistant_message.message_id,
                context_metadata=context_metadata,
                summary_id=summary_id,
                memory_ids=pinned_memory_ids,
                profile_id=preferred_profile_id,
                model_metadata=model_metadata,
            )
            return ConversationTurnView(
                conversation=stored,
                user_message=user_message,
                assistant_message=assistant_message,
                tool_call=stored_tool,
                tool_calls=_turn_loop_steps(stored, user_message),
            )

        with capture_work_timings() as timings:
            request_started = timings.clock()
            return await self._idempotent_async(
                scope=f"conversations:{conversation_id}:"
                + ("work-confirmation" if _confirmation is not None else "turns"),
                key=durable_idempotency_key,
                request=_confirmation if _confirmation is not None else request,
                response_type=ConversationTurnView,
                prepare=prepare,
                finalize=finalize,
                legacy_key=(
                    idempotency_key
                    if durable_idempotency_key != idempotency_key
                    else None
                ),
                legacy_request_hashes=(
                    (
                        content_hash(
                            request.model_dump(
                                mode="json",
                                exclude={"analysis_intent_task_id", "start_new_analysis_goal"},
                            )
                        ),
                    )
                    if conversation.mode is ConversationMode.WORK
                    and _confirmation is None
                    and not {
                        "analysis_intent_task_id",
                        "start_new_analysis_goal",
                    }.intersection(request.model_fields_set)
                    else ()
                ),
            )

    def _commit_preprocessing_work_task_binding(
        self,
        *,
        conversation: ConversationView,
        user_content: str | None,
        payload: dict[str, Any],
        project_id: str | None,
        bind_run: bool = False,
    ) -> str | None:
        """Bind only committed preprocessing Plans and successfully created Runs."""

        def plan_identity(value: Any) -> tuple[str, str] | None:
            if not isinstance(value, dict):
                return None
            plan_id = value.get("plan_revision_id")
            plan_body = value.get("plan")
            skill_plan = plan_body.get("skill_plan") if isinstance(plan_body, dict) else None
            project = value.get("project_id") or project_id
            if (
                not isinstance(plan_id, str)
                or not isinstance(project, str)
                or not isinstance(plan_body, dict)
                or plan_body.get("kind") != "skill_plan"
                or not isinstance(skill_plan, dict)
                or not isinstance(skill_plan.get("preprocessing_parameters_hash"), str)
            ):
                return None
            return plan_id, project

        def goal_description(plan_revision_id: str) -> str:
            goal_turns: list[str] = []
            messages = conversation.messages

            def is_preprocessing_turn(payload: dict[str, Any]) -> bool:
                if payload.get("agent_role") == WorkAgentRole.PREPROCESSING.value:
                    return True
                if isinstance(payload.get("work_draft"), dict):
                    return True
                return plan_identity(payload.get("plan_revision")) is not None

            def is_operation_command(content: str) -> bool:
                normalized = content.strip()
                if normalized in {
                    "已在参数面板保存修改。",
                    "重新校验当前方案与启动确认。",
                }:
                    return True
                return bool(
                    re.fullmatch(r"确认方案参数 [a-f0-9]{12}", normalized)
                    or re.fullmatch(
                        r"批准并启动 [a-f0-9]{12} (?:做头动QC|跳过QC) [a-f0-9-]{36}",
                        normalized,
                    )
                    or re.match(r"^(?:确认启动|重新校验当前方案|查看运行进度|查看 QC)", normalized)
                )

            for index, message in enumerate(messages[:-1]):
                if message.role is not ConversationRole.USER:
                    continue
                assistant = messages[index + 1]
                if (
                    assistant.role is ConversationRole.ASSISTANT
                    and is_preprocessing_turn(assistant.payload)
                    and not is_operation_command(message.content)
                ):
                    content = message.content.strip()
                    if content and content not in goal_turns:
                        goal_turns.append(content)
            content = (user_content or "").strip()
            if content and not is_operation_command(content) and content not in goal_turns:
                goal_turns.append(content)
            if not goal_turns:
                # The fallback describes the authoritative Plan without
                # borrowing a request from another Work role or inventing
                # additional scientific intent.
                goal_turns.append(f"执行并核验静息态 fMRI 预处理方案 {plan_revision_id}。")
            return ("用户目标：\n" + "\n".join(f"- {item}" for item in goal_turns))[-4000:]

        def analysis_intent_binding() -> tuple[str | None, str | None]:
            draft_envelope = payload.get("work_draft")
            envelope = payload.get("analysis_intent_envelope")
            task_id = (
                draft_envelope.get("analysis_intent_task_id")
                if isinstance(draft_envelope, dict)
                else None
            )
            request_hash = (
                draft_envelope.get("analysis_request_hash")
                if isinstance(draft_envelope, dict)
                else None
            )
            if (
                task_id is None
                and isinstance(envelope, dict)
                and envelope.get("status") == "active"
                and envelope.get("conversation_id") == conversation.conversation_id
                and envelope.get("project_id") in {None, project_id}
            ):
                task_id = envelope.get("task_id")
                request_hash = envelope.get("analysis_request_hash")
            return (
                task_id if isinstance(task_id, str) else None,
                request_hash if isinstance(request_hash, str) else None,
            )

        task: Any | None = None
        plan = plan_identity(payload.get("plan_revision"))
        if plan is not None:
            plan_id, effective_project_id = plan
            intent_task_id, intent_request_hash = analysis_intent_binding()
            task = self.automation.ensure_preprocessing_plan_task(
                conversation_id=conversation.conversation_id,
                project_id=effective_project_id,
                plan_revision_id=plan_id,
                goal_description=goal_description(plan_id),
                analysis_intent_task_id=intent_task_id,
                analysis_request_hash=intent_request_hash,
            )

        if not bind_run:
            return task.task_id if task is not None else None

        run_data = payload.get("run")
        if not isinstance(run_data, dict):
            return task.task_id if task is not None else None
        run_id = run_data.get("run_id")
        run_plan_revision_id = run_data.get("plan_revision_id")
        run_project_id = run_data.get("project_id") or project_id
        if (
            not isinstance(run_id, str)
            or not isinstance(run_plan_revision_id, str)
            or not isinstance(run_project_id, str)
        ):
            raise ConflictError(
                "work_task_run_binding_missing", "新建的预处理运行缺少可核验的项目或方案引用。"
            )
        run_plan = self.get_plan(run_plan_revision_id)
        run_skill_plan = run_plan.plan.get("skill_plan")
        if (
            run_plan.project_id != run_project_id
            or run_plan.plan.get("kind") != "skill_plan"
            or not isinstance(run_skill_plan, dict)
            or not isinstance(run_skill_plan.get("preprocessing_parameters_hash"), str)
        ):
            return None
        linked = conversation_work_task(conversation, WorkAgentRole.PREPROCESSING)
        if (
            linked is not None
            and linked.work_task_id is not None
            and linked.plan_revision_id == run_plan_revision_id
        ):
            task_id = linked.work_task_id
        elif task is not None and task.authority_refs.plan_revision_id == run_plan_revision_id:
            task_id = task.task_id
        else:
            # A legacy conversation may have a valid, previously compiled Plan
            # but no durable task link. Create the record only after a Run has
            # actually been created in this same transaction.
            intent_task_id, intent_request_hash = analysis_intent_binding()
            task = self.automation.ensure_preprocessing_plan_task(
                conversation_id=conversation.conversation_id,
                project_id=run_project_id,
                plan_revision_id=run_plan_revision_id,
                goal_description=goal_description(run_plan_revision_id),
                analysis_intent_task_id=intent_task_id,
                analysis_request_hash=intent_request_hash,
            )
            task_id = task.task_id
        self.automation.bind_preprocessing_run(
            task_id=task_id,
            conversation_id=conversation.conversation_id,
            project_id=run_project_id,
            plan_revision_id=run_plan_revision_id,
            run_id=run_id,
        )
        bound_task = self.automation.get_task(task_id)
        intent_id = bound_task.authority_refs.analysis_intent_task_id
        intent = None
        if intent_id is not None:
            intent = next(
                (
                    message.payload.get("analysis_dependency")
                    for message in conversation.messages
                    if isinstance(message.payload.get("analysis_dependency"), dict)
                    and message.payload["analysis_dependency"].get("task_id") == intent_id
                    and message.payload["analysis_dependency"].get("conversation_id")
                    == conversation.conversation_id
                    and message.payload["analysis_dependency"].get("project_id") == run_project_id
                    and content_hash(
                        str(
                            message.payload["analysis_dependency"].get("analysis_request", "")
                        ).strip()
                    )
                    == bound_task.authority_refs.analysis_request_hash
                ),
                None,
            )
            if intent is None:
                raise ConflictError(
                    "analysis_intent_source_missing",
                    "持久预处理任务引用的复合分析原请求缺失或身份摘要不匹配。",
                )
        self.automation.ensure_analysis_proposal_outbox(
            intent=intent if isinstance(intent, dict) else None,
            conversation_id=conversation.conversation_id,
            project_id=run_project_id,
            preprocessing_task_id=task_id,
        )
        return task_id

    def _commit_analysis_work_task_binding(
        self,
        *,
        conversation: ConversationView,
        user_content: str | None,
        payload: dict[str, Any],
        project_id: str | None,
    ) -> str | None:
        """Persist a statistical WorkTask only for a committed server proposal."""

        proposal = payload.get("statistical_design_proposal")
        draft = payload.get("analysis_draft")
        if not isinstance(proposal, dict) or not isinstance(draft, dict):
            return None
        proposal_hash = proposal.get("proposal_hash")
        draft_hash_value = draft.get("hash")
        effective_project_id = proposal.get("project_id") or project_id
        continuation = payload.get("analysis_outbox_continuation")
        draft_intent_id = draft.get("analysis_intent_task_id")
        draft_request_hash = draft.get("analysis_request_hash")
        continuation_intent_id = (
            continuation.get("intent_task_id") if isinstance(continuation, dict) else None
        )
        continuation_request_hash = (
            continuation.get("analysis_request_hash") if isinstance(continuation, dict) else None
        )
        analysis_intent_task_id = draft_intent_id or continuation_intent_id
        analysis_request_hash = draft_request_hash or continuation_request_hash
        design = proposal.get("design")
        proposal_external_import_id = (
            design.get("external_import_id") if isinstance(design, dict) else None
        )
        proposal_external_import_hash = (
            design.get("external_import_hash") if isinstance(design, dict) else None
        )
        if (
            not isinstance(proposal_hash, str)
            or not re.fullmatch(r"[a-f0-9]{64}", proposal_hash)
            or not isinstance(draft_hash_value, str)
            or not re.fullmatch(r"[a-f0-9]{64}", draft_hash_value)
            or not isinstance(effective_project_id, str)
        ):
            return None
        if (
            draft_intent_id is not None
            and continuation_intent_id is not None
            and draft_intent_id != continuation_intent_id
        ) or (
            draft_request_hash is not None
            and continuation_request_hash is not None
            and draft_request_hash != continuation_request_hash
        ):
            raise ConflictError(
                "work_task_composite_goal_conflict",
                "统计草稿与 outbox continuation 声明了不同的复合目标身份。",
            )
        # Older non-composite outboxes and adapters may include their local
        # continuation task ID without a frozen composite request hash. Such
        # an ID is not sufficient authority to associate a WorkTask. If a
        # composite root exists, the repository's terminal outbox transaction
        # below requires the complete identity and rolls back this task bind.
        has_composite_hash = draft_request_hash is not None or continuation_request_hash is not None
        if has_composite_hash and (
            analysis_intent_task_id is None or analysis_request_hash is None
        ):
            raise ConflictError(
                "work_task_composite_goal_identity_missing",
                "统计草稿的复合目标身份不完整，无法绑定分析任务。",
            )
        if not has_composite_hash:
            analysis_intent_task_id = None
            analysis_request_hash = None
        if analysis_intent_task_id is not None and (
            not isinstance(analysis_intent_task_id, str)
            or not isinstance(analysis_request_hash, str)
            or not re.fullmatch(r"[a-f0-9]{64}", analysis_request_hash)
        ):
            raise ConflictError(
                "work_task_composite_goal_identity_invalid",
                "统计草稿的复合目标引用格式无效。",
            )

        goal_turns: list[str] = []
        for index, message in enumerate(conversation.messages[:-1]):
            if message.role is not ConversationRole.USER:
                continue
            assistant = conversation.messages[index + 1]
            if assistant.role is not ConversationRole.ASSISTANT:
                continue
            previous_payload = assistant.payload
            if previous_payload.get("agent_role") != WorkAgentRole.ANALYSIS.value:
                continue
            if not (
                isinstance(previous_payload.get("analysis_draft"), dict)
                or isinstance(previous_payload.get("statistical_design_proposal"), dict)
            ):
                continue
            content = message.content.strip()
            if content and content not in goal_turns:
                goal_turns.append(content)
        current_content = (user_content or "").strip()
        if current_content and current_content not in goal_turns:
            goal_turns.append(current_content)
        if not goal_turns:
            design = proposal.get("design")
            test_name = design.get("test") if isinstance(design, dict) else None
            goal_turns.append(
                "依据已审查的统计设计提案准备并核验静息态 fMRI 统计结果"
                + (f"（{test_name}）" if isinstance(test_name, str) else "")
                + f" {proposal_hash[:12]}。"
            )
        return self.automation.ensure_analysis_proposal_task(
            conversation_id=conversation.conversation_id,
            project_id=effective_project_id,
            proposal_hash=proposal_hash,
            draft_hash=draft_hash_value,
            goal_description=("用户目标：\n" + "\n".join(f"- {item}" for item in goal_turns))[
                -4000:
            ],
            analysis_intent_task_id=(
                analysis_intent_task_id if isinstance(analysis_intent_task_id, str) else None
            ),
            analysis_request_hash=(
                analysis_request_hash if isinstance(analysis_request_hash, str) else None
            ),
            source_run_id=(
                proposal.get("source_run_id")
                if isinstance(proposal.get("source_run_id"), str)
                else None
            ),
            input_manifest_hash=(
                proposal.get("input_manifest_hash")
                if isinstance(proposal.get("input_manifest_hash"), str)
                else None
            ),
            external_import_id=(
                proposal_external_import_id
                if isinstance(proposal_external_import_id, str)
                else None
            ),
            external_import_hash=(
                proposal_external_import_hash
                if isinstance(proposal_external_import_hash, str)
                else None
            ),
        ).task_id

    async def _send_chat_turn(
        self,
        conversation_id: str,
        request: ConversationTurnCreate,
        idempotency_key: str,
    ) -> ConversationTurnView:
        async def prepare() -> tuple[
            str,
            dict[str, Any],
            dict[str, Any],
            str | None,
        ]:
            conversation = self.repository.get_conversation(conversation_id)
            preferred_profile_id = (
                request.preferred_profile_id
                if request.preferred_profile_id is not None
                else conversation.preferred_profile_id
            )
            prepared_context = await self.conversation_context.prepare_chat(
                conversation,
                question=request.content,
                preferred_profile_id=preferred_profile_id,
                model=request.model,
            )
            preferred_profile_id = prepared_context.profile_id
            try:
                result = await self.chat_agent.respond(
                    ChatAgentRequest(
                        session_id=conversation_id,
                        message=request.content,
                        stream=request.stream,
                        paper_ids=request.paper_ids,
                        recent_messages=prepared_context.recent_messages,
                        pinned_context=prepared_context.pinned_context,
                        conversation_summary=(
                            prepared_context.summary.content if prepared_context.summary else None
                        ),
                        context_window_tokens=prepared_context.context_window_tokens,
                        max_output_tokens=prepared_context.max_output_tokens,
                        preferred_profile_id=preferred_profile_id,
                        model=request.model,
                        allow_remote_search=request.allow_remote_search,
                    )
                )
            except (ChatAgentError, ContextBudgetError) as exc:
                code = getattr(exc, "code", "context_input_too_large")
                message = getattr(exc, "message", "当前消息超过所选模型的上下文预算。")
                raise InputValidationError(code, message) from exc
            evidence = tuple(
                EvidenceChunk(
                    source=citation.source,
                    title=citation.title,
                    excerpt=citation.excerpt,
                    score=1.0,
                )
                for citation in result.citations
            )
            answer = RsFmriAnswer(
                in_scope=bool(result.metadata.get("in_scope", True)),
                answer=result.answer,
                evidence=evidence,
            )
            view = RsFmriAnswerView(
                answer=answer,
                remote_search_used=bool(result.metadata.get("remote_search_used", False)),
            )
            payload = {
                "rag": view.model_dump(mode="json"),
                "chat": result.model_dump(mode="json"),
                "context_summary_id": (
                    prepared_context.summary.summary_id if prepared_context.summary else None
                ),
                "memory_ids": list(prepared_context.memory_ids),
                "memory_recall": prepared_context.memory_recall,
                "memory_candidates": [
                    item.model_dump(mode="json") for item in prepared_context.memory_candidates
                ],
                "memory_extraction": prepared_context.memory_extraction,
            }
            model_metadata = result.metadata.get("model")
            if isinstance(model_metadata, dict):
                payload["model"] = model_metadata
                preferred_profile_id = str(model_metadata["profile_id"])
            tool_name = (
                "rsfmri_chat_llm"
                if result.metadata.get("llm_used")
                else "work_request_draft"
                if result.work_request is not None
                else "rag_rsfmri_question"
            )
            tool = self._tool_result(
                tool_name,
                {
                    "question": request.content,
                    "allow_remote_search": request.allow_remote_search,
                    "model_profile_id": preferred_profile_id,
                    "model": request.model,
                },
                payload,
            )
            return result.answer, payload, tool, preferred_profile_id

        def finalize(
            prepared: tuple[str, dict[str, Any], dict[str, Any], str | None],
        ) -> ConversationTurnView:
            return self._persist_chat_turn(conversation_id, request, prepared)

        return await self._idempotent_async(
            scope=f"conversations:{conversation_id}:turns",
            key=idempotency_key,
            request=request,
            response_type=ConversationTurnView,
            prepare=prepare,
            finalize=finalize,
        )

    def _persist_chat_turn(
        self,
        conversation_id: str,
        request: ConversationTurnCreate,
        prepared: tuple[str, dict[str, Any], dict[str, Any], str | None],
    ) -> ConversationTurnView:
        assistant_content, payload, tool, preferred_profile_id = prepared
        conversation = self.repository.get_conversation(conversation_id)
        loop_calls = _loop_tool_calls(payload)
        stored, user_message, assistant_message, stored_tool = (
            self.repository.append_conversation_exchange(
                conversation_id,
                user_content=request.content,
                assistant_content=assistant_content,
                assistant_payload=payload,
                tool=tool,
                tool_calls=loop_calls,
                workspace_path=conversation.workspace_path,
                preferred_profile_id=preferred_profile_id,
                project_id=conversation.project_id,
                active_run_id=conversation.active_run_id,
            )
        )
        candidates = tuple(
            MemoryCandidateDraft.model_validate(item)
            for item in payload.get("memory_candidates", [])
        )
        candidate_ids = self.conversation_context.persist_memory_candidates(
            conversation_id, candidates, user_message.message_id
        )
        if candidate_ids:
            self.event_store.append_event(
                project_id=conversation.project_id,
                run_id=conversation.active_run_id,
                event_type="MemoryCandidatesUpdated",
                severity="info",
                payload={"conversation_id": conversation_id, "memory_ids": candidate_ids},
            )
        context_metadata = payload.get("chat", {}).get("metadata", {}).get("context", {})
        model_metadata = payload.get("model")
        context_hash = self.conversation_context.persist_snapshot(
            conversation_id,
            assistant_message_id=assistant_message.message_id,
            context_metadata=context_metadata,
            summary_id=payload.get("context_summary_id"),
            memory_ids=payload.get("memory_ids", []),
            profile_id=preferred_profile_id,
            model_metadata=model_metadata if isinstance(model_metadata, dict) else None,
        )
        self.event_store.append_event(
            project_id=conversation.project_id,
            run_id=conversation.active_run_id,
            event_type="ContextBuilt",
            severity="info",
            payload={"conversation_id": conversation_id, "context_hash": context_hash},
        )
        return ConversationTurnView(
            conversation=stored,
            user_message=user_message,
            assistant_message=assistant_message,
            tool_call=stored_tool,
            tool_calls=_turn_loop_steps(stored, user_message),
        )

    @staticmethod
    def _tool_result(
        name: str, input_data: dict[str, Any], output: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "tool_name": name,
            "status": ConversationToolStatus.SUCCEEDED.value,
            "input": input_data,
            "output": output,
        }


def _turn_loop_steps(
    conversation: ConversationView, user_message: ConversationMessageView
) -> list[ConversationToolCallView]:
    """The Agent tool-loop steps this turn produced, in execution order.

    Shared by the Chat and Work paths so both report the same contract.  Only
    rows carrying a ``step_index`` are loop steps; the turn's summary tool record
    is reported separately through ``tool_call``.
    """

    return [
        item
        for item in conversation.tool_calls
        if item.user_message_id == user_message.message_id and item.step_index is not None
    ]


def _loop_tool_calls(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract bounded-tool-loop audit records from a turn payload.

    Chat nests them under ``chat.metadata.tool_loop``; Work keeps ``tool_loop`` at
    the top level of its payload.  Both shapes are accepted.  The records were
    produced by the dispatcher, so their arguments and results are already
    redacted and bounded; malformed entries are dropped rather than trusted,
    because this payload has been persisted and re-read before.
    """

    loop: Any = payload.get("tool_loop")
    if not isinstance(loop, dict):
        chat = payload.get("chat")
        metadata = chat.get("metadata") if isinstance(chat, dict) else None
        loop = metadata.get("tool_loop") if isinstance(metadata, dict) else None
    if not isinstance(loop, dict):
        return []
    raw_calls = loop.get("calls")
    if not isinstance(raw_calls, list):
        return []

    calls: list[dict[str, Any]] = []
    for entry in raw_calls:
        if not isinstance(entry, dict):
            continue
        name = entry.get("tool_name")
        status = entry.get("status")
        if not isinstance(name, str) or not name or not isinstance(status, str):
            continue
        calls.append(
            {
                "tool_name": name,
                "status": status,
                "input": entry.get("input") if isinstance(entry.get("input"), dict) else {},
                "output": entry.get("output") if isinstance(entry.get("output"), dict) else {},
                "error": entry.get("error") if isinstance(entry.get("error"), str) else None,
                "step_index": entry.get("step_index"),
                "risk_tier": entry.get("risk_tier"),
                "provider_call_id": entry.get("provider_call_id"),
                "duration_ms": entry.get("duration_ms"),
                "agent_role": loop.get("agent_role") or payload.get("agent_role"),
            }
        )
    return calls
