"""Durable Agent conversations, messages, and tool-call audit records."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select, update

from neuroagent.application.contracts.conversations import (
    ConfirmationChallengeView,
    ContextSummaryView,
    ConversationContextView,
    ConversationMessageView,
    ConversationMode,
    ConversationRole,
    ConversationToolCallView,
    ConversationToolStatus,
    ConversationView,
    ConversationWorkTaskView,
    MemoryAction,
    MemoryCreate,
    MemoryKind,
    MemoryScope,
    MemoryStatus,
    MemoryUpdate,
    MemoryView,
    WorkAgentSelection,
)
from neuroagent.application.errors import ConflictError, InputValidationError, NotFoundError
from neuroagent.application.hashing import canonical_json
from neuroagent.application.work_tasks import require_analysis_task
from neuroagent.infrastructure.persistence.models import (
    ConfirmationChallengeRow,
    ContextSnapshotRow,
    ContextSummaryRow,
    ConversationMessageRow,
    ConversationRow,
    ConversationToolCallRow,
    MemoryEmbeddingRow,
    MemoryRecordRow,
    PlanRevisionRow,
    ProjectRow,
    WorkflowRunRow,
)
from neuroagent.infrastructure.persistence.repository_mixins._base import (
    RepositoryBaseMixin,
    _as_utc,
    _id,
    _load,
)
from neuroagent.multi_agent.registry import WorkAgentRole


class ConversationMixin(RepositoryBaseMixin):
    def select_work_agent(
        self, conversation_id: str, request: WorkAgentSelection
    ) -> ConversationView:
        with self._write_session() as session:
            row = session.get(ConversationRow, conversation_id)
            if row is None:
                raise NotFoundError("conversation", conversation_id)
            if row.mode != "work":
                raise InputValidationError(
                    "work_agent_mode_required", "仅 Work 会话可以切换负责 Agent。"
                )
            self._check_version("conversation", row.version, request.expected_version)
            row.foreground_agent_role = request.agent_role.value
            tasks = _load(row.work_tasks_json, {})
            row.active_run_id = tasks.get(request.agent_role.value, {}).get("run_id")
            row.version += 1
            session.flush()
            return self._conversation_view(session, row)

    def bind_statistics_task(
        self,
        *,
        conversation_id: str,
        project_id: str,
        proposal_hash: str,
        plan_revision_id: str,
        run_id: str | None = None,
        work_task_id: str | None = None,
    ) -> ConversationView:
        with self._write_session() as session:
            row = session.get(ConversationRow, conversation_id)
            if row is None:
                raise NotFoundError("conversation", conversation_id)
            task = require_analysis_task(
                self._conversation_view(session, row),
                project_id=project_id,
                proposal_hash=proposal_hash,
                plan_revision_id=plan_revision_id if run_id else None,
            )
            plan = session.get(PlanRevisionRow, plan_revision_id)
            if plan is None or plan.project_id != project_id:
                raise ConflictError("statistics_work_project_mismatch", "方案不属于此会话项目。")
            if run_id:
                run = session.get(WorkflowRunRow, run_id)
                if (
                    run is None
                    or run.project_id != project_id
                    or run.plan_revision_id != plan_revision_id
                ):
                    raise ConflictError(
                        "statistics_work_run_mismatch", "运行与当前统计方案不匹配。"
                    )
            tasks = _load(row.work_tasks_json, {})
            updates: dict[str, Any] = {
                "plan_revision_id": plan_revision_id,
                **({"run_id": run_id} if run_id else {}),
            }
            if work_task_id is not None:
                updates["work_task_id"] = work_task_id
            tasks["analysis"] = task.model_copy(update=updates).model_dump(mode="json")
            row.work_tasks_json = canonical_json(tasks)
            if run_id:
                row.active_run_id = run_id
            row.version += 1
            session.flush()
            return self._conversation_view(session, row)

    @staticmethod
    def _confirmation_challenge(row: ConfirmationChallengeRow) -> ConfirmationChallengeView:
        from neuroagent.application.contracts.runs import QcExecutionMode

        return ConfirmationChallengeView(
            confirmation_id=row.confirmation_id,
            conversation_id=row.conversation_id,
            project_id=row.project_id,
            plan_revision_id=row.plan_revision_id,
            plan_hash=row.plan_hash,
            qc_mode=QcExecutionMode(row.qc_mode),
            issued_at=_as_utc(row.issued_at),
            expires_at=_as_utc(row.expires_at),
            consumed_at=_as_utc(row.consumed_at) if row.consumed_at else None,
            consumed_run_id=row.consumed_run_id,
        )

    def create_confirmation_challenge(
        self,
        *,
        conversation_id: str,
        project_id: str,
        plan_revision_id: str,
        plan_hash: str,
        qc_mode: Any,
        ttl_seconds: int = 600,
    ) -> ConfirmationChallengeView:
        with self._write_session() as session:
            conversation = session.get(ConversationRow, conversation_id)
            if conversation is None:
                raise NotFoundError("conversation", conversation_id)
            if conversation.project_id is not None and conversation.project_id != project_id:
                raise ConflictError("confirmation_project_mismatch", "确认挑战与当前项目不匹配。")
            now = datetime.now(UTC)
            row = ConfirmationChallengeRow(
                confirmation_id=_id(),
                conversation_id=conversation_id,
                project_id=project_id,
                plan_revision_id=plan_revision_id,
                plan_hash=plan_hash,
                qc_mode=str(getattr(qc_mode, "value", qc_mode)),
                issued_at=now,
                expires_at=now + timedelta(seconds=max(30, min(ttl_seconds, 3600))),
            )
            session.add(row)
            session.flush()
            return self._confirmation_challenge(row)

    def consume_confirmation_challenge(
        self,
        *,
        confirmation_id: str,
        conversation_id: str,
        project_id: str,
        plan_revision_id: str,
        plan_hash: str,
        qc_mode: Any,
        run_id: str,
    ) -> ConfirmationChallengeView:
        with self._write_session() as session:
            row = session.get(ConfirmationChallengeRow, confirmation_id)
            expected_qc = str(getattr(qc_mode, "value", qc_mode))
            now = datetime.now(UTC)
            if (
                row is None
                or row.conversation_id != conversation_id
                or row.project_id != project_id
                or row.plan_revision_id != plan_revision_id
                or row.plan_hash != plan_hash
                or row.qc_mode != expected_qc
                or row.consumed_at is not None
                or _as_utc(row.expires_at) <= now
            ):
                raise ConflictError(
                    "confirmation_challenge_invalid",
                    "启动确认无效、已使用、已过期或与当前方案不匹配。",
                )
            row.consumed_at = now
            row.consumed_run_id = run_id
            session.flush()
            return self._confirmation_challenge(row)

    def create_conversation(
        self,
        *,
        mode: ConversationMode,
        title: str,
        welcome: str,
        workspace_path: str | None,
        preferred_profile_id: str | None,
        project_id: str | None = None,
    ) -> ConversationView:
        with self._write_session() as session:
            if project_id is not None and session.get(ProjectRow, project_id) is None:
                raise NotFoundError("project", project_id)
            row = ConversationRow(
                conversation_id=_id(),
                mode=mode.value,
                title=title,
                workspace_path=workspace_path,
                preferred_profile_id=preferred_profile_id,
                project_id=project_id,
                version=1,
            )
            session.add(row)
            session.flush()
            session.add(
                ConversationMessageRow(
                    message_id=_id(),
                    conversation_id=row.conversation_id,
                    sequence=1,
                    role=ConversationRole.ASSISTANT.value,
                    content=welcome,
                    payload_json="{}",
                )
            )
            session.flush()
            return self._conversation_view(session, row)

    def get_conversation(self, conversation_id: str) -> ConversationView:
        with self._read_session() as session:
            row = session.get(ConversationRow, conversation_id)
            if row is None:
                raise NotFoundError("conversation", conversation_id)
            return self._conversation_view(session, row)

    def list_conversations(self, *, mode: ConversationMode | None = None) -> list[ConversationView]:
        with self._read_session() as session:
            statement = select(ConversationRow)
            if mode is not None:
                statement = statement.where(ConversationRow.mode == mode.value)
            rows = session.scalars(statement.order_by(ConversationRow.updated_at.desc())).all()
            return [self._conversation_view(session, row) for row in rows]

    def append_conversation_exchange(
        self,
        conversation_id: str,
        *,
        user_content: str,
        assistant_content: str,
        assistant_payload: dict[str, Any],
        tool: dict[str, Any] | None,
        workspace_path: str | None,
        preferred_profile_id: str | None,
        project_id: str | None,
        active_run_id: str | None,
        tool_calls: Sequence[dict[str, Any]] | None = None,
        work_task_id: str | None = None,
    ) -> tuple[
        ConversationView,
        ConversationMessageView,
        ConversationMessageView,
        ConversationToolCallView | None,
    ]:
        """Append one user/assistant exchange and its audit rows atomically.

        ``tool`` is the long-standing single-call form; ``tool_calls`` carries the
        ordered steps of a bounded Agent tool loop.  Writing them in the same
        transaction as the messages means a loop's audit trail can never be
        half-persisted, and it avoids needing the user message id before insert.
        """
        with self._write_session() as session:
            row = session.get(ConversationRow, conversation_id)
            if row is None:
                raise NotFoundError("conversation", conversation_id)
            last_sequence = session.scalar(
                select(func.max(ConversationMessageRow.sequence)).where(
                    ConversationMessageRow.conversation_id == conversation_id
                )
            )
            user_row = ConversationMessageRow(
                message_id=_id(),
                conversation_id=conversation_id,
                sequence=int(last_sequence or 0) + 1,
                role=ConversationRole.USER.value,
                content=user_content,
                payload_json="{}",
            )
            assistant_row = ConversationMessageRow(
                message_id=_id(),
                conversation_id=conversation_id,
                sequence=user_row.sequence + 1,
                role=ConversationRole.ASSISTANT.value,
                content=assistant_content,
                payload_json=canonical_json(assistant_payload),
            )
            session.add_all((user_row, assistant_row))
            session.flush()
            tool_row: ConversationToolCallRow | None = None
            if tool is not None:
                tool_row = ConversationToolCallRow(
                    tool_call_id=_id(),
                    conversation_id=conversation_id,
                    user_message_id=user_row.message_id,
                    tool_name=str(tool["tool_name"]),
                    status=str(tool["status"]),
                    input_json=canonical_json(tool.get("input", {})),
                    output_json=canonical_json(tool.get("output", {})),
                    error=tool.get("error"),
                    agent_role=tool.get("agent_role"),
                )
                session.add(tool_row)
            for step_index, call in enumerate(tool_calls or (), start=1):
                session.add(
                    ConversationToolCallRow(
                        tool_call_id=_id(),
                        conversation_id=conversation_id,
                        user_message_id=user_row.message_id,
                        tool_name=str(call["tool_name"]),
                        status=str(call["status"]),
                        input_json=canonical_json(call.get("input", {})),
                        output_json=canonical_json(call.get("output", {})),
                        error=call.get("error"),
                        step_index=int(call["step_index"])
                        if call.get("step_index") is not None
                        else step_index,
                        risk_tier=str(call["risk_tier"])
                        if call.get("risk_tier") is not None
                        else None,
                        provider_call_id=str(call["provider_call_id"])
                        if call.get("provider_call_id") is not None
                        else None,
                        duration_ms=int(call["duration_ms"])
                        if call.get("duration_ms") is not None
                        else None,
                        agent_role=str(call["agent_role"])
                        if call.get("agent_role") is not None
                        else None,
                    )
                )
            row.workspace_path = workspace_path
            row.preferred_profile_id = preferred_profile_id
            tasks = _load(row.work_tasks_json, {}) if row.project_id == project_id else {}
            row.project_id = project_id
            row.active_run_id = active_run_id
            if row.mode == "work":
                role = WorkAgentRole(assistant_payload.get("agent_role", "preprocessing"))
                row.foreground_agent_role = role.value
                task = ConversationWorkTaskView.model_validate(
                    tasks.get(role.value, {"agent_role": role.value})
                )
                snapshot = assistant_payload.get(
                    "analysis_draft" if role is WorkAgentRole.ANALYSIS else "work_draft"
                )
                if (
                    isinstance(snapshot, dict)
                    and isinstance(snapshot.get("hash"), str)
                    and snapshot["hash"] != task.draft_hash
                ):
                    task = task.model_copy(
                        update={
                            "draft_hash": snapshot["hash"],
                            "proposal_hash": None,
                            "proposal_message_id": None,
                            "plan_revision_id": None,
                            "work_task_id": None,
                            "run_id": None,
                        }
                    )
                if role in {WorkAgentRole.PREPROCESSING, WorkAgentRole.ANALYSIS} and (
                    work_task_id is not None
                ):
                    task = task.model_copy(update={"work_task_id": work_task_id})
                plan_data = assistant_payload.get("plan_revision")
                if role is WorkAgentRole.PREPROCESSING and isinstance(plan_data, dict):
                    plan_id = plan_data.get("plan_revision_id")
                    plan = (
                        session.get(PlanRevisionRow, plan_id) if isinstance(plan_id, str) else None
                    )
                    plan_body = _load(plan.plan_json, {}) if plan is not None else {}
                    skill_plan = plan_body.get("skill_plan")
                    if (
                        plan is not None
                        and plan.project_id == project_id
                        and plan_body.get("kind") == "skill_plan"
                        and isinstance(skill_plan, dict)
                        and isinstance(skill_plan.get("preprocessing_parameters_hash"), str)
                    ):
                        changes: dict[str, Any] = {"plan_revision_id": plan.plan_revision_id}
                        if task.plan_revision_id != plan.plan_revision_id:
                            changes.update(
                                {
                                    "run_id": None,
                                    "work_task_id": work_task_id,
                                }
                            )
                        task = task.model_copy(update=changes)
                proposal = assistant_payload.get("statistical_design_proposal")
                if (
                    role is WorkAgentRole.ANALYSIS
                    and isinstance(proposal, dict)
                    and proposal.get("proposal_hash") != task.proposal_hash
                ):
                    task = task.model_copy(
                        update={
                            "plan_revision_id": None,
                            "run_id": None,
                            "proposal_hash": proposal["proposal_hash"],
                            "proposal_message_id": assistant_row.message_id,
                            "work_task_id": work_task_id,
                        }
                    )
                # An analysis turn may refer to an upstream run; it must never
                # replace the analysis run reference with that preprocessing source.
                if active_run_id:
                    run = session.get(WorkflowRunRow, active_run_id)
                    plan = session.get(PlanRevisionRow, run.plan_revision_id) if run else None
                    is_statistics = (
                        plan is not None
                        and _load(plan.plan_json, {}).get("kind") == "statistical_design"
                    )
                    if (
                        run is not None
                        and run.project_id == project_id
                        and task.plan_revision_id in {None, run.plan_revision_id}
                        and (role is WorkAgentRole.ANALYSIS) == is_statistics
                    ):
                        task = task.model_copy(
                            update={
                                "run_id": active_run_id,
                                "plan_revision_id": run.plan_revision_id,
                            }
                        )
                tasks[role.value] = task.model_dump(mode="json")
                row.work_tasks_json = canonical_json(tasks)
                row.active_run_id = task.run_id
            row.version += 1
            session.flush()
            return (
                self._conversation_view(session, row),
                self._message(user_row),
                self._message(assistant_row),
                self._tool_call(tool_row) if tool_row is not None else None,
            )

    def get_conversation_context(self, conversation_id: str) -> ConversationContextView:
        with self._read_session() as session:
            conversation = session.get(ConversationRow, conversation_id)
            if conversation is None:
                raise NotFoundError("conversation", conversation_id)
            summary_row = session.scalars(
                select(ContextSummaryRow)
                .where(ContextSummaryRow.conversation_id == conversation_id)
                .order_by(ContextSummaryRow.covered_sequence.desc())
            ).first()
            memory_filter = MemoryRecordRow.conversation_id == conversation_id
            if conversation.project_id is not None:
                memory_filter = or_(
                    memory_filter,
                    (MemoryRecordRow.scope == MemoryScope.PROJECT.value)
                    & (MemoryRecordRow.project_id == conversation.project_id),
                )
            rows = session.scalars(
                select(MemoryRecordRow)
                .where(
                    memory_filter,
                    MemoryRecordRow.status.in_(
                        [MemoryStatus.PENDING.value, MemoryStatus.CONFIRMED.value]
                    ),
                )
                .order_by(MemoryRecordRow.pinned.desc(), MemoryRecordRow.updated_at.desc())
            ).all()
            indexed_versions = {
                item.memory_id: item.memory_version
                for item in session.scalars(
                    select(MemoryEmbeddingRow).where(
                        MemoryEmbeddingRow.memory_id.in_([row.memory_id for row in rows])
                    )
                ).all()
            }
            return ConversationContextView(
                summary=self._summary(summary_row) if summary_row else None,
                memories=[
                    self._memory(item).model_copy(
                        update={
                            "semantic_indexed": indexed_versions.get(item.memory_id) == item.version
                        }
                    )
                    for item in rows
                ],
            )

    def create_memory(self, conversation_id: str, request: MemoryCreate) -> MemoryView:
        with self._write_session() as session:
            conversation = session.get(ConversationRow, conversation_id)
            if conversation is None:
                raise NotFoundError("conversation", conversation_id)
            if request.scope is MemoryScope.PROJECT and conversation.project_id is None:
                raise InputValidationError(
                    "project_context_required", "项目记忆要求对话已绑定项目。"
                )
            if request.source_message_id is not None:
                source = session.get(ConversationMessageRow, request.source_message_id)
                if (
                    source is None
                    or source.conversation_id != conversation_id
                    or source.role != "user"
                ):
                    raise InputValidationError(
                        "memory_source_invalid",
                        "Memory source must be a user message in this conversation.",
                    )
            scope_filter = (
                (MemoryRecordRow.scope == MemoryScope.CONVERSATION.value)
                & (MemoryRecordRow.conversation_id == conversation_id)
                if request.scope is MemoryScope.CONVERSATION
                else (MemoryRecordRow.scope == MemoryScope.PROJECT.value)
                & (MemoryRecordRow.project_id == conversation.project_id)
            )
            existing = session.scalars(
                select(MemoryRecordRow).where(
                    scope_filter,
                    MemoryRecordRow.kind == request.kind.value,
                    MemoryRecordRow.key == request.key,
                )
            ).first()
            if existing is not None:
                if (
                    existing.status == "confirmed"
                    and existing.content == request.content
                    and existing.pinned == request.pinned
                    and existing.importance == request.importance
                    and (_as_utc(existing.expires_at) if existing.expires_at else None)
                    == request.expires_at
                    and existing.source_message_id == request.source_message_id
                ):
                    return self._memory(existing)
                raise ConflictError(
                    "memory_key_conflict",
                    "A memory with this key exists; explicitly update or resolve it.",
                    memory_id=existing.memory_id,
                    version=existing.version,
                )
            row = MemoryRecordRow(
                importance=request.importance,
                expires_at=request.expires_at,
                memory_id=_id(),
                conversation_id=conversation_id,
                project_id=(
                    conversation.project_id if request.scope is MemoryScope.PROJECT else None
                ),
                scope=request.scope.value,
                kind=request.kind.value,
                key=request.key,
                content=request.content,
                status=MemoryStatus.CONFIRMED.value,
                pinned=request.pinned,
                source_message_id=request.source_message_id,
                confidence=1.0,
                version=1,
            )
            session.add(row)
            session.flush()
            return self._memory(row)

    def upsert_memory_candidate(
        self,
        conversation_id: str,
        *,
        kind: MemoryKind,
        key: str,
        content: str,
        status: MemoryStatus,
        pinned: bool,
        confidence: float,
        source_message_id: str,
        scope: MemoryScope = MemoryScope.CONVERSATION,
        importance: float = 0.5,
    ) -> MemoryView:
        with self._write_session() as session:
            conversation = session.get(ConversationRow, conversation_id)
            if conversation is None:
                raise NotFoundError("conversation", conversation_id)
            source = session.get(ConversationMessageRow, source_message_id)
            if source is None or source.conversation_id != conversation_id or source.role != "user":
                raise InputValidationError(
                    "memory_source_invalid",
                    "Memory source must be a user message in this conversation.",
                )
            if scope is MemoryScope.PROJECT and conversation.project_id is None:
                raise InputValidationError(
                    "project_context_required",
                    "Project memory requires a project-bound conversation.",
                )
            scope_filter = (
                (MemoryRecordRow.scope == MemoryScope.CONVERSATION.value)
                & (MemoryRecordRow.conversation_id == conversation_id)
                if scope is MemoryScope.CONVERSATION
                else (MemoryRecordRow.scope == MemoryScope.PROJECT.value)
                & (MemoryRecordRow.project_id == conversation.project_id)
            )
            row = session.scalars(
                select(MemoryRecordRow).where(
                    scope_filter,
                    MemoryRecordRow.kind == kind.value,
                    MemoryRecordRow.key == key,
                )
            ).first()
            if row is None:
                row = MemoryRecordRow(
                    memory_id=_id(),
                    conversation_id=conversation_id,
                    project_id=conversation.project_id if scope is MemoryScope.PROJECT else None,
                    scope=scope.value,
                    kind=kind.value,
                    key=key,
                    content=content,
                    status=status.value,
                    pinned=pinned,
                    confidence=confidence,
                    importance=importance,
                    source_message_id=source_message_id,
                    version=1,
                )
                session.add(row)
            elif row.status == MemoryStatus.PENDING.value:
                row.content = content
                row.status = status.value
                row.pinned = pinned
                row.confidence = confidence
                row.importance = importance
                row.source_message_id = source_message_id
                row.version += 1
            elif row.status == MemoryStatus.CONFIRMED.value and row.content != content:
                row.proposed_content = content
                row.proposal_source_message_id = source_message_id
                row.version += 1
                session.execute(
                    update(MemoryEmbeddingRow)
                    .where(MemoryEmbeddingRow.memory_id == row.memory_id)
                    .values(memory_version=row.version)
                )
            session.flush()
            return self._memory(row)

    def get_forgotten_source_message_ids(self, conversation_id: str) -> set[str]:
        with self._read_session() as session:
            source_ids = session.scalars(
                select(MemoryRecordRow.source_message_id).where(
                    MemoryRecordRow.conversation_id == conversation_id,
                    MemoryRecordRow.status == MemoryStatus.FORGOTTEN.value,
                    MemoryRecordRow.source_message_id.is_not(None),
                )
            ).all()
            return {source_id for source_id in source_ids if source_id is not None}

    def update_memory(
        self, conversation_id: str, memory_id: str, request: MemoryUpdate
    ) -> MemoryView:
        with self._write_session() as session:
            conversation = session.get(ConversationRow, conversation_id)
            if conversation is None:
                raise NotFoundError("conversation", conversation_id)
            row = session.get(MemoryRecordRow, memory_id)
            accessible = row is not None and (
                row.conversation_id == conversation_id
                or (
                    row.scope == MemoryScope.PROJECT.value
                    and row.project_id == conversation.project_id
                )
            )
            if not accessible or row is None:
                raise NotFoundError("memory", memory_id)
            if row.version != request.expected_version:
                raise ConflictError(
                    "revision_conflict",
                    "记忆版本已变化,请刷新后重试。",
                    expected=request.expected_version,
                    actual=row.version,
                )
            embedding_still_matches = False
            if request.action is MemoryAction.CONFIRM:
                row.status = MemoryStatus.CONFIRMED.value
            elif request.action is MemoryAction.UPDATE:
                if request.content is None and not (
                    {"importance", "expires_at"} & request.model_fields_set
                ):
                    raise InputValidationError("memory_content_required", "修改记忆需要内容。")
                if request.content is not None:
                    row.content = request.content
                if request.importance is not None:
                    row.importance = request.importance
                if "expires_at" in request.model_fields_set:
                    row.expires_at = request.expires_at
                row.status = MemoryStatus.CONFIRMED.value
                embedding_still_matches = request.content is None
            elif request.action is MemoryAction.REJECT:
                row.status = MemoryStatus.REJECTED.value
                row.pinned = False
            elif request.action is MemoryAction.PIN:
                row.pinned = True
                embedding_still_matches = True
            elif request.action is MemoryAction.UNPIN:
                row.pinned = False
                embedding_still_matches = True
            elif request.action is MemoryAction.ACCEPT_PROPOSAL:
                if row.proposed_content is None:
                    raise InputValidationError(
                        "memory_proposal_missing", "No memory proposal exists."
                    )
                row.content = row.proposed_content
                row.source_message_id = row.proposal_source_message_id
                row.proposed_content = None
                row.proposal_source_message_id = None
            elif request.action is MemoryAction.MERGE_PROPOSAL:
                if row.proposed_content is None or request.content is None:
                    raise InputValidationError(
                        "memory_merge_content_required", "Merged memory content is required."
                    )
                row.content = request.content
                row.source_message_id = row.proposal_source_message_id
                row.proposed_content = None
                row.proposal_source_message_id = None
            elif request.action is MemoryAction.REJECT_PROPOSAL:
                if row.proposed_content is None:
                    raise InputValidationError(
                        "memory_proposal_missing", "No memory proposal exists."
                    )
                row.proposed_content = None
                row.proposal_source_message_id = None
                embedding_still_matches = True
            else:
                row.status = MemoryStatus.FORGOTTEN.value
                row.content = ""
                row.pinned = False
                row.proposed_content = None
                row.proposal_source_message_id = None
                if row.source_message_id is not None:
                    sequence = session.scalar(
                        select(ConversationMessageRow.sequence).where(
                            ConversationMessageRow.message_id == row.source_message_id
                        )
                    )
                    if sequence is not None:
                        session.execute(
                            delete(ContextSummaryRow).where(
                                ContextSummaryRow.conversation_id == row.conversation_id,
                                ContextSummaryRow.covered_sequence >= sequence,
                            )
                        )
            row.version += 1
            if embedding_still_matches:
                session.execute(
                    update(MemoryEmbeddingRow)
                    .where(MemoryEmbeddingRow.memory_id == memory_id)
                    .values(memory_version=row.version)
                )
            else:
                session.execute(
                    delete(MemoryEmbeddingRow).where(MemoryEmbeddingRow.memory_id == memory_id)
                )
            session.flush()
            return self._memory(row)

    def store_memory_embedding(
        self,
        conversation_id: str,
        memory_id: str,
        *,
        expected_version: int,
        model_identity: str,
        vector: tuple[float, ...],
    ) -> bool:
        from neuroagent.memory.semantic import cosine

        if cosine(vector, vector) is None:
            raise InputValidationError("invalid_memory_vector", "Invalid memory embedding.")
        with self._write_session() as session:
            conversation = session.get(ConversationRow, conversation_id)
            memory = session.get(MemoryRecordRow, memory_id)
            if conversation is None or memory is None:
                raise NotFoundError("memory", memory_id)
            accessible = memory.conversation_id == conversation_id or (
                memory.scope == "project"
                and conversation.project_id is not None
                and memory.project_id == conversation.project_id
            )
            if not accessible:
                raise NotFoundError("memory", memory_id)
            if (
                memory.version != expected_version
                or memory.status != "confirmed"
                or (
                    memory.expires_at is not None
                    and _as_utc(memory.expires_at) <= datetime.now(UTC)
                )
            ):
                return False
            embedding = session.get(MemoryEmbeddingRow, memory_id)
            if embedding is None:
                session.add(
                    MemoryEmbeddingRow(
                        memory_id=memory_id,
                        memory_version=expected_version,
                        model_identity=model_identity,
                        vector_json=canonical_json(list(vector)),
                    )
                )
            else:
                embedding.memory_version = expected_version
                embedding.model_identity = model_identity
                embedding.vector_json = canonical_json(list(vector))
            return True

    def get_memory_embeddings(
        self,
        conversation_id: str,
    ) -> dict[str, tuple[int, str, tuple[float, ...]]]:
        state = self.get_conversation_context(conversation_id)
        versions = {
            item.memory_id: item.version
            for item in state.memories
            if item.status is MemoryStatus.CONFIRMED
        }
        with self._read_session() as session:
            rows = session.scalars(
                select(MemoryEmbeddingRow).where(
                    MemoryEmbeddingRow.memory_id.in_(versions),
                )
            ).all()
            return {
                row.memory_id: (
                    row.memory_version,
                    row.model_identity,
                    tuple(_load(row.vector_json, [])),
                )
                for row in rows
                if row.memory_version == versions[row.memory_id]
            }

    def create_context_summary(
        self,
        conversation_id: str,
        *,
        content: str,
        covered_sequence: int,
        source_hash: str,
        method: str,
    ) -> ContextSummaryView:
        with self._write_session() as session:
            if session.get(ConversationRow, conversation_id) is None:
                raise NotFoundError("conversation", conversation_id)
            row = ContextSummaryRow(
                summary_id=_id(),
                conversation_id=conversation_id,
                content=content,
                covered_sequence=covered_sequence,
                source_hash=source_hash,
                method=method,
            )
            session.add(row)
            session.flush()
            return self._summary(row)

    def create_context_snapshot(
        self,
        conversation_id: str,
        *,
        assistant_message_id: str | None,
        context_hash: str,
        profile_id: str | None,
        manifest: dict[str, Any],
    ) -> None:
        with self._write_session() as session:
            session.add(
                ContextSnapshotRow(
                    snapshot_id=_id(),
                    conversation_id=conversation_id,
                    assistant_message_id=assistant_message_id,
                    context_hash=context_hash,
                    profile_id=profile_id,
                    manifest_json=canonical_json(manifest),
                )
            )

    @classmethod
    def _conversation_view(cls, session: Any, row: ConversationRow) -> ConversationView:
        messages = session.scalars(
            select(ConversationMessageRow)
            .where(ConversationMessageRow.conversation_id == row.conversation_id)
            .order_by(ConversationMessageRow.sequence)
        ).all()
        tools = session.scalars(
            select(ConversationToolCallRow)
            .where(ConversationToolCallRow.conversation_id == row.conversation_id)
            .order_by(
                ConversationToolCallRow.created_at,
                # Loop steps share one timestamp, so order them by their step so
                # the transcript the model produced is the transcript shown.
                ConversationToolCallRow.step_index,
                ConversationToolCallRow.tool_call_id,
            )
        ).all()
        return ConversationView(
            conversation_id=row.conversation_id,
            mode=ConversationMode(row.mode),
            title=row.title,
            workspace_path=row.workspace_path,
            preferred_profile_id=row.preferred_profile_id,
            project_id=row.project_id,
            active_run_id=row.active_run_id,
            foreground_agent_role=WorkAgentRole(row.foreground_agent_role),
            work_tasks=tuple(
                ConversationWorkTaskView.model_validate(value)
                for value in _load(row.work_tasks_json, {}).values()
            ),
            version=row.version,
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
            messages=[cls._message(message) for message in messages],
            tool_calls=[cls._tool_call(item) for item in tools],
        )

    @staticmethod
    def _message(row: ConversationMessageRow) -> ConversationMessageView:
        return ConversationMessageView(
            message_id=row.message_id,
            conversation_id=row.conversation_id,
            sequence=row.sequence,
            role=ConversationRole(row.role),
            content=row.content,
            payload=_load(row.payload_json, {}),
            created_at=_as_utc(row.created_at),
        )

    @staticmethod
    def _tool_call(row: ConversationToolCallRow) -> ConversationToolCallView:
        return ConversationToolCallView(
            tool_call_id=row.tool_call_id,
            conversation_id=row.conversation_id,
            user_message_id=row.user_message_id,
            tool_name=row.tool_name,
            status=ConversationToolStatus(row.status),
            input=_load(row.input_json, {}),
            output=_load(row.output_json, {}),
            error=row.error,
            step_index=row.step_index,
            risk_tier=row.risk_tier,
            duration_ms=row.duration_ms,
            agent_role=row.agent_role,
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
        )

    @staticmethod
    def _memory(row: MemoryRecordRow) -> MemoryView:
        return MemoryView(
            importance=row.importance,
            expires_at=_as_utc(row.expires_at) if row.expires_at is not None else None,
            proposed_content=row.proposed_content,
            proposal_source_message_id=row.proposal_source_message_id,
            memory_id=row.memory_id,
            conversation_id=row.conversation_id,
            project_id=row.project_id,
            scope=MemoryScope(row.scope),
            kind=MemoryKind(row.kind),
            key=row.key,
            content=row.content,
            status=MemoryStatus(row.status),
            pinned=row.pinned,
            source_message_id=row.source_message_id,
            confidence=row.confidence,
            version=row.version,
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
        )

    @staticmethod
    def _summary(row: ContextSummaryRow) -> ContextSummaryView:
        return ContextSummaryView(
            summary_id=row.summary_id,
            conversation_id=row.conversation_id,
            content=row.content,
            covered_sequence=row.covered_sequence,
            source_hash=row.source_hash,
            method=row.method,
            created_at=_as_utc(row.created_at),
        )
