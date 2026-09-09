"""Durable Agent conversations, messages, and tool-call audit records."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select

from neuroagent.application.contracts import (
    ContextSummaryView,
    ConversationContextView,
    ConversationMessageView,
    ConversationMode,
    ConversationRole,
    ConversationToolCallView,
    ConversationToolStatus,
    ConversationView,
    MemoryAction,
    MemoryCreate,
    MemoryKind,
    MemoryScope,
    MemoryStatus,
    MemoryUpdate,
    MemoryView,
)
from neuroagent.application.errors import ConflictError, InputValidationError, NotFoundError
from neuroagent.application.hashing import canonical_json
from neuroagent.infrastructure.persistence.models import (
    ContextSnapshotRow,
    ContextSummaryRow,
    ConversationMessageRow,
    ConversationRow,
    ConversationToolCallRow,
    MemoryRecordRow,
)
from neuroagent.infrastructure.persistence.repository_mixins._base import (
    RepositoryBaseMixin,
    _as_utc,
    _id,
    _load,
)


class ConversationMixin(RepositoryBaseMixin):
    def create_conversation(
        self,
        *,
        mode: ConversationMode,
        title: str,
        welcome: str,
        workspace_path: str | None,
        preferred_profile_id: str | None,
    ) -> ConversationView:
        with self._write_session() as session:
            row = ConversationRow(
                conversation_id=_id(),
                mode=mode.value,
                title=title,
                workspace_path=workspace_path,
                preferred_profile_id=preferred_profile_id,
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
        with self.database.session_factory() as session:
            row = session.get(ConversationRow, conversation_id)
            if row is None:
                raise NotFoundError("conversation", conversation_id)
            return self._conversation_view(session, row)

    def list_conversations(self, *, mode: ConversationMode | None = None) -> list[ConversationView]:
        with self.database.session_factory() as session:
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
    ) -> tuple[
        ConversationView,
        ConversationMessageView,
        ConversationMessageView,
        ConversationToolCallView | None,
    ]:
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
                )
                session.add(tool_row)
            row.workspace_path = workspace_path
            row.preferred_profile_id = preferred_profile_id
            row.project_id = project_id
            row.active_run_id = active_run_id
            row.version += 1
            session.flush()
            return (
                self._conversation_view(session, row),
                self._message(user_row),
                self._message(assistant_row),
                self._tool_call(tool_row) if tool_row is not None else None,
            )

    def get_conversation_context(self, conversation_id: str) -> ConversationContextView:
        with self.database.session_factory() as session:
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
            return ConversationContextView(
                summary=self._summary(summary_row) if summary_row else None,
                memories=[self._memory(item) for item in rows],
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
            row = MemoryRecordRow(
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
    ) -> MemoryView:
        with self._write_session() as session:
            conversation = session.get(ConversationRow, conversation_id)
            if conversation is None:
                raise NotFoundError("conversation", conversation_id)
            row = session.scalars(
                select(MemoryRecordRow).where(
                    MemoryRecordRow.conversation_id == conversation_id,
                    MemoryRecordRow.scope == MemoryScope.CONVERSATION.value,
                    MemoryRecordRow.kind == kind.value,
                    MemoryRecordRow.key == key,
                )
            ).first()
            if row is None:
                row = MemoryRecordRow(
                    memory_id=_id(),
                    conversation_id=conversation_id,
                    project_id=None,
                    scope=MemoryScope.CONVERSATION.value,
                    kind=kind.value,
                    key=key,
                    content=content,
                    status=status.value,
                    pinned=pinned,
                    confidence=confidence,
                    version=1,
                )
                session.add(row)
            elif row.status != MemoryStatus.CONFIRMED.value:
                row.content = content
                row.status = status.value
                row.pinned = pinned
                row.confidence = confidence
                row.version += 1
            session.flush()
            return self._memory(row)

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
            if request.action is MemoryAction.CONFIRM:
                row.status = MemoryStatus.CONFIRMED.value
            elif request.action is MemoryAction.UPDATE:
                if request.content is None:
                    raise InputValidationError("memory_content_required", "修改记忆需要内容。")
                row.content = request.content
                row.status = MemoryStatus.CONFIRMED.value
            elif request.action is MemoryAction.REJECT:
                row.status = MemoryStatus.REJECTED.value
                row.pinned = False
            elif request.action is MemoryAction.PIN:
                row.pinned = True
            elif request.action is MemoryAction.UNPIN:
                row.pinned = False
            else:
                row.status = MemoryStatus.FORGOTTEN.value
                row.content = ""
                row.pinned = False
            row.version += 1
            session.flush()
            return self._memory(row)

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
            .order_by(ConversationToolCallRow.created_at)
        ).all()
        return ConversationView(
            conversation_id=row.conversation_id,
            mode=ConversationMode(row.mode),
            title=row.title,
            workspace_path=row.workspace_path,
            preferred_profile_id=row.preferred_profile_id,
            project_id=row.project_id,
            active_run_id=row.active_run_id,
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
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
        )

    @staticmethod
    def _memory(row: MemoryRecordRow) -> MemoryView:
        return MemoryView(
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
