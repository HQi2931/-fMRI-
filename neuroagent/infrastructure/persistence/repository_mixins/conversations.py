"""Durable Agent conversations, messages, and tool-call audit records."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from neuroagent.application.contracts import (
    ConversationMessageView,
    ConversationMode,
    ConversationRole,
    ConversationToolCallView,
    ConversationToolStatus,
    ConversationView,
)
from neuroagent.application.errors import NotFoundError
from neuroagent.application.hashing import canonical_json
from neuroagent.infrastructure.persistence.models import (
    ConversationMessageRow,
    ConversationRow,
    ConversationToolCallRow,
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
