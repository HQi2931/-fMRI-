"""Application-layer coordination for bounded conversation context."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from neuroagent.agent.gateway import ChatGatewayResult
from neuroagent.application.contracts import (
    ContextSummaryView,
    ConversationContextView,
    ConversationView,
    MemoryKind,
    MemoryStatus,
    ModelProfileView,
)
from neuroagent.application.errors import ApplicationError
from neuroagent.application.ports import RepositoryPort
from neuroagent.context.engine import ContextBudgetError, ContextEngine
from neuroagent.context.interfaces import ContextMessage, ContextPacket, PinnedContext


@dataclass(frozen=True, slots=True)
class PreparedChatContext:
    """Context selected for one Chat turn before the model is called."""

    profile_id: str | None
    recent_messages: tuple[ContextMessage, ...]
    pinned_context: tuple[PinnedContext, ...]
    summary: ContextSummaryView | None
    context_window_tokens: int
    max_output_tokens: int
    memory_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreparedWorkContext:
    """Context selected for one deterministic Work turn."""

    profile_id: str | None
    packet: ContextPacket
    state: ConversationContextView


class ConversationContextCoordinator:
    """Keep context policy and persistence out of conversation use-case orchestration."""

    def __init__(
        self,
        repository: RepositoryPort,
        engine: ContextEngine,
        summary_generator: Callable[..., Awaitable[ChatGatewayResult]],
    ) -> None:
        self._repository = repository
        self._engine = engine
        self._summary_generator = summary_generator

    def context_limits(self, preferred_profile_id: str | None) -> tuple[int, int]:
        profiles = self._repository.list_model_profiles()
        profile = self._select_profile(profiles, preferred_profile_id)
        if profile is None:
            return 16_384, 2_048
        return profile.context_window_tokens, profile.max_output_tokens

    async def prepare_chat(
        self,
        conversation: ConversationView,
        *,
        question: str,
        preferred_profile_id: str | None,
        model: str | None,
    ) -> PreparedChatContext:
        self.capture_memory_candidates(conversation.conversation_id, question)
        profile_id = preferred_profile_id
        context_window_tokens, max_output_tokens = self.context_limits(profile_id)
        messages = self.messages_from(conversation)
        summary = await self._prepare_summary(
            conversation.conversation_id,
            messages,
            preferred_profile_id=profile_id,
            model=model,
            context_window_tokens=context_window_tokens,
            max_output_tokens=max_output_tokens,
        )
        state = self._repository.get_conversation_context(conversation.conversation_id)
        recent_messages = tuple(
            item
            for item in messages
            if summary is None or item.sequence is None or item.sequence > summary.covered_sequence
        )
        return PreparedChatContext(
            profile_id=profile_id,
            recent_messages=recent_messages,
            pinned_context=self.pinned_context(state),
            summary=summary,
            context_window_tokens=context_window_tokens,
            max_output_tokens=max_output_tokens,
            memory_ids=tuple(item.memory_id for item in state.memories if item.pinned),
        )

    def prepare_work(
        self,
        conversation: ConversationView,
        *,
        question: str,
        preferred_profile_id: str | None,
        project_id: str | None,
        active_run_id: str | None,
        plan_revision_id: str | None,
    ) -> PreparedWorkContext:
        self.capture_memory_candidates(conversation.conversation_id, question)
        profile_id = preferred_profile_id
        context_window_tokens, max_output_tokens = self.context_limits(profile_id)
        self._prepare_work_summary(
            conversation,
            context_window_tokens=context_window_tokens,
            max_output_tokens=max_output_tokens,
        )
        state = self._repository.get_conversation_context(conversation.conversation_id)
        packet = self._engine.build(
            question=question,
            recent_messages=self.messages_from(conversation),
            retrieval_context=(),
            pinned_context=self.pinned_context(state),
            conversation_summary=state.summary.content if state.summary else None,
            context_window_tokens=context_window_tokens,
            max_output_tokens=max_output_tokens,
            context_kind="work",
            work_context=self.work_context(
                project_id=project_id,
                active_run_id=active_run_id,
                plan_revision_id=plan_revision_id,
            ),
        )
        return PreparedWorkContext(profile_id=profile_id, packet=packet, state=state)

    def work_context(
        self,
        *,
        project_id: str | None,
        active_run_id: str | None,
        plan_revision_id: str | None,
    ) -> dict[str, Any]:
        context: dict[str, Any] = {"project_id": project_id}
        selected_plan_id = plan_revision_id
        if active_run_id is not None:
            run = self._repository.get_run(active_run_id)
            selected_plan_id = run.plan_revision_id
            artifacts = self._repository.list_artifacts(run.run_id)
            qc_review = self._repository.get_latest_qc_review_for_run(run.run_id)
            context["run"] = {
                "run_id": run.run_id,
                "state": run.state.value,
                "attempt": run.attempt,
                "cancel_requested": run.cancel_requested,
                "artifact_count": len(artifacts),
                "artifact_types": sorted({item.artifact_type for item in artifacts}),
            }
            if qc_review is not None:
                context["qc"] = {
                    "review_revision_id": qc_review.review.review_revision_id,
                    "revision": qc_review.revision,
                    "state": qc_review.state.value,
                    "content_hash": qc_review.review.content_hash,
                    "included_subject_count": len(qc_review.review.included_subject_ids),
                    "excluded_subject_count": len(qc_review.review.excluded_subject_ids),
                }
        if selected_plan_id is not None:
            plan = self._repository.get_plan(selected_plan_id)
            context["plan"] = {
                "plan_revision_id": plan.plan_revision_id,
                "state": plan.state.value,
                "plan_hash": plan.plan_hash,
                "blocking_issue_count": sum(
                    item.severity == "blocking" for item in plan.validation_issues
                ),
            }
        return context

    def persist_snapshot(
        self,
        conversation_id: str,
        *,
        assistant_message_id: str | None,
        context_metadata: dict[str, Any],
        summary_id: str | None,
        memory_ids: list[str],
        profile_id: str | None,
        model_metadata: dict[str, Any] | None = None,
    ) -> str:
        context_hash = self._context_hash(context_metadata, model_metadata)
        redaction_count = model_metadata.get("redaction_count", 0) if model_metadata else 0
        self._repository.create_context_snapshot(
            conversation_id,
            assistant_message_id=assistant_message_id,
            context_hash=context_hash,
            profile_id=(
                str(model_metadata["profile_id"])
                if model_metadata and model_metadata.get("profile_id")
                else profile_id
            ),
            manifest={
                "selection": context_metadata,
                "summary_id": summary_id,
                "memory_ids": memory_ids,
                "redaction_count": redaction_count,
            },
        )
        return context_hash

    def capture_memory_candidates(self, conversation_id: str, content: str) -> None:
        preferences = (
            (r"(?:请|以后)?用英文(?:回答)?", "language", "使用英文回答"),
            (r"(?:请|以后)?用中文(?:回答)?", "language", "使用中文回答"),
            (r"(?:回答|回复).{0,4}(?:简洁|简短)", "answer_length", "回答保持简洁"),
            (r"(?:回答|回复).{0,4}(?:详细|展开)", "answer_length", "回答提供详细解释"),
            (r"(?:使用|用).{0,4}(?:Markdown|表格)", "format", "优先使用结构化格式"),
        )
        for pattern, key, value in preferences:
            if re.search(pattern, content, re.IGNORECASE):
                self._repository.upsert_memory_candidate(
                    conversation_id,
                    kind=MemoryKind.PREFERENCE,
                    key=key,
                    content=value,
                    status=MemoryStatus.CONFIRMED,
                    pinned=True,
                    confidence=1.0,
                )
        band = re.search("(0?\\.\\d+)\\s*[-\\u2013\\u2014至到,]\\s*(0?\\.\\d+)", content)
        if band and re.search(r"ALFF|fALFF|滤波|频段", content, re.IGNORECASE):
            self._repository.upsert_memory_candidate(
                conversation_id,
                kind=MemoryKind.SCIENTIFIC_PARAMETER,
                key="frequency_band",
                content=f"频段 {band.group(1)}-{band.group(2)} Hz",
                status=MemoryStatus.PENDING,
                pinned=False,
                confidence=0.9,
            )
        smoothing = re.search(
            r"(?:平滑|smooth\w*)\D{0,8}(\d+(?:\.\d+)?)\s*mm",
            content,
            re.IGNORECASE,
        )
        if smoothing:
            self._repository.upsert_memory_candidate(
                conversation_id,
                kind=MemoryKind.SCIENTIFIC_PARAMETER,
                key="smoothing_fwhm",
                content=f"平滑核 {smoothing.group(1)} mm",
                status=MemoryStatus.PENDING,
                pinned=False,
                confidence=0.9,
            )
        if re.search(r"(?:排除|剔除).{0,30}(?:受试者|被试|FD|头动|帧)", content, re.IGNORECASE):
            self._repository.upsert_memory_candidate(
                conversation_id,
                kind=MemoryKind.SCIENTIFIC_PARAMETER,
                key="exclusion_rule",
                content=content[:240],
                status=MemoryStatus.PENDING,
                pinned=False,
                confidence=0.8,
            )
        method = re.search(
            r"(?:采用|使用|选择)\s*(ALFF|fALFF|ReHo|种子点相关|ICA)",
            content,
            re.IGNORECASE,
        )
        if method:
            self._repository.upsert_memory_candidate(
                conversation_id,
                kind=MemoryKind.SCIENTIFIC_PARAMETER,
                key="method_selection",
                content=f"方法选择 {method.group(1)}",
                status=MemoryStatus.PENDING,
                pinned=False,
                confidence=0.9,
            )

    @staticmethod
    def messages_from(conversation: ConversationView) -> tuple[ContextMessage, ...]:
        return tuple(
            ContextMessage(
                role=item.role.value,
                content=item.content,
                message_id=item.message_id,
                sequence=item.sequence,
            )
            for item in conversation.messages
            if item.role.value in {"user", "assistant"}
        )

    @staticmethod
    def pinned_context(state: ConversationContextView) -> tuple[PinnedContext, ...]:
        return tuple(
            PinnedContext(
                key=item.key,
                value=item.content,
                priority="high" if item.pinned else "medium",
                source_message_id=item.source_message_id,
            )
            for item in state.memories
            if item.status is MemoryStatus.CONFIRMED
        )

    async def _prepare_summary(
        self,
        conversation_id: str,
        messages: tuple[ContextMessage, ...],
        *,
        preferred_profile_id: str | None,
        model: str | None,
        context_window_tokens: int,
        max_output_tokens: int,
    ) -> ContextSummaryView | None:
        state = self._repository.get_conversation_context(conversation_id)
        covered = state.summary.covered_sequence if state.summary else 0
        if not self._engine.should_summarize(
            messages,
            covered_sequence=covered,
            context_window_tokens=context_window_tokens,
            max_output_tokens=max_output_tokens,
        ):
            return state.summary
        older = tuple(item for item in messages[:-12] if (item.sequence or 0) > covered)
        if not older:
            return state.summary
        method = "model"
        try:
            generated = await self._summary_generator(
                question="请更新这段会话的上下文摘要。",
                evidence=[],
                recent_messages=[item.model_dump() for item in older],
                pinned_context=[],
                conversation_summary=state.summary.content if state.summary else None,
                preferred_profile_id=preferred_profile_id,
                model=model,
                allow_web_search=False,
                summary_mode=True,
            )
            parsed = json.loads(generated.response.content)
            summary_content = str(parsed["summary"]).strip()
        except (ApplicationError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            method = "extractive"
            previous = f"{state.summary.content}\n" if state.summary else ""
            summary_content = previous + self._engine.extractive_summary(older)
        return self._store_summary(
            conversation_id,
            summary_content=summary_content,
            older=older,
            covered_sequence=int(older[-1].sequence or covered),
            method=method,
        )

    def _prepare_work_summary(
        self,
        conversation: ConversationView,
        *,
        context_window_tokens: int,
        max_output_tokens: int,
    ) -> None:
        messages = self.messages_from(conversation)
        state = self._repository.get_conversation_context(conversation.conversation_id)
        covered = state.summary.covered_sequence if state.summary else 0
        if not self._engine.should_summarize(
            messages,
            covered_sequence=covered,
            context_window_tokens=context_window_tokens,
            max_output_tokens=max_output_tokens,
        ):
            return
        older = tuple(item for item in messages[:-12] if (item.sequence or 0) > covered)
        if not older:
            return
        previous = f"{state.summary.content}\n" if state.summary else ""
        self._store_summary(
            conversation.conversation_id,
            summary_content=previous + self._engine.extractive_summary(older),
            older=older,
            covered_sequence=int(older[-1].sequence or covered),
            method="extractive",
        )

    def _store_summary(
        self,
        conversation_id: str,
        *,
        summary_content: str,
        older: tuple[ContextMessage, ...],
        covered_sequence: int,
        method: str,
    ) -> ContextSummaryView:
        source_hash = hashlib.sha256(
            json.dumps(
                [item.model_dump(mode="json") for item in older],
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        summary = self._repository.create_context_summary(
            conversation_id,
            content=summary_content[:8_000],
            covered_sequence=covered_sequence,
            source_hash=source_hash,
            method=method,
        )
        self._repository.append_event(
            project_id=None,
            run_id=None,
            event_type="ContextCompressed",
            severity="info",
            payload={
                "conversation_id": conversation_id,
                "covered_sequence": covered_sequence,
                "method": method,
            },
        )
        return summary

    @staticmethod
    def _select_profile(profiles: list[ModelProfileView], preferred_profile_id: str | None) -> Any:
        if preferred_profile_id is not None:
            return next(
                (item.profile for item in profiles if item.profile.id == preferred_profile_id),
                None,
            )
        return profiles[0].profile if profiles else None

    @staticmethod
    def _context_hash(
        context_metadata: dict[str, Any], model_metadata: dict[str, Any] | None
    ) -> str:
        if model_metadata and model_metadata.get("context_hash"):
            return str(model_metadata["context_hash"])
        return hashlib.sha256(
            json.dumps(context_metadata, sort_keys=True).encode("utf-8")
        ).hexdigest()


__all__ = [
    "ContextBudgetError",
    "ConversationContextCoordinator",
    "PreparedChatContext",
    "PreparedWorkContext",
]
