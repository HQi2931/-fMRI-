"""Persistence for workspace file consent and immutable change proposals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from neuroagent.application.contracts import (
    WorkspaceChangeProposal,
    WorkspaceCitation,
    WorkspaceContentConsent,
    WorkspaceEdit,
)
from neuroagent.application.errors import ConflictError, NotFoundError
from neuroagent.application.hashing import canonical_json
from neuroagent.infrastructure.persistence.models import (
    ConversationRow,
    WorkspaceConsentRow,
    WorkspacePatchRow,
)
from neuroagent.infrastructure.persistence.repository_mixins._base import (
    RepositoryBaseMixin,
    _as_utc,
    _id,
    _load,
)


class WorkspaceAssistantMixin(RepositoryBaseMixin):
    @staticmethod
    def _consent(row: WorkspaceConsentRow) -> WorkspaceContentConsent:
        return WorkspaceContentConsent(
            consent_id=row.consent_id,
            conversation_id=row.conversation_id,
            workspace_hash=row.workspace_hash,
            profile_id=row.profile_id,
            profile_revision=row.profile_revision,
            model=row.model,
            policy_version=row.policy_version,
            provider_host=row.provider_host,
            granted_at=_as_utc(row.granted_at),
            revoked_at=_as_utc(row.revoked_at) if row.revoked_at else None,
        )

    @staticmethod
    def _proposal(row: WorkspacePatchRow) -> WorkspaceChangeProposal:
        return WorkspaceChangeProposal(
            proposal_id=row.proposal_id,
            conversation_id=row.conversation_id,
            workspace_hash=row.workspace_hash,
            proposal_hash=row.proposal_hash,
            edits=tuple(WorkspaceEdit.model_validate(item) for item in _load(row.edits_json, [])),
            diff=row.diff_text,
            citations=tuple(
                WorkspaceCitation.model_validate(item) for item in _load(row.citations_json, [])
            ),
            status=row.status,
            expires_at=_as_utc(row.expires_at),
            applied_at=_as_utc(row.applied_at) if row.applied_at else None,
            undone_at=_as_utc(row.undone_at) if row.undone_at else None,
        )

    def create_workspace_consent(
        self,
        *,
        conversation_id: str,
        workspace_hash: str,
        profile_id: str,
        profile_revision: str,
        model: str,
        policy_version: str,
        provider_host: str,
    ) -> WorkspaceContentConsent:
        with self._write_session() as session:
            conversation = session.get(ConversationRow, conversation_id)
            if conversation is None:
                raise NotFoundError("conversation", conversation_id)
            existing = session.scalars(
                select(WorkspaceConsentRow).where(
                    WorkspaceConsentRow.conversation_id == conversation_id,
                    WorkspaceConsentRow.workspace_hash == workspace_hash,
                    WorkspaceConsentRow.profile_id == profile_id,
                    WorkspaceConsentRow.model == model,
                    WorkspaceConsentRow.policy_version == policy_version,
                )
            ).first()
            if existing is not None:
                existing.profile_revision = profile_revision
                existing.provider_host = provider_host
                existing.granted_at = datetime.now(UTC)
                existing.revoked_at = None
                session.flush()
                return self._consent(existing)
            row = WorkspaceConsentRow(
                consent_id=_id(),
                conversation_id=conversation_id,
                workspace_hash=workspace_hash,
                profile_id=profile_id,
                profile_revision=profile_revision,
                model=model,
                policy_version=policy_version,
                provider_host=provider_host,
            )
            session.add(row)
            session.flush()
            return self._consent(row)

    def list_workspace_consents(self, conversation_id: str) -> list[WorkspaceContentConsent]:
        with self._read_session() as session:
            if session.get(ConversationRow, conversation_id) is None:
                raise NotFoundError("conversation", conversation_id)
            rows = session.scalars(
                select(WorkspaceConsentRow)
                .where(WorkspaceConsentRow.conversation_id == conversation_id)
                .order_by(WorkspaceConsentRow.granted_at.desc())
            ).all()
            return [self._consent(row) for row in rows]

    def revoke_workspace_consent(
        self, conversation_id: str, consent_id: str
    ) -> WorkspaceContentConsent:
        with self._write_session() as session:
            row = session.get(WorkspaceConsentRow, consent_id)
            if row is None or row.conversation_id != conversation_id:
                raise NotFoundError("workspace_consent", consent_id)
            row.revoked_at = datetime.now(UTC)
            session.flush()
            return self._consent(row)

    def create_workspace_patch(
        self,
        *,
        conversation_id: str,
        workspace_hash: str,
        proposal_hash: str,
        edits: list[dict[str, Any]],
        diff: str,
        citations: list[dict[str, Any]],
        ttl_seconds: int = 1800,
    ) -> WorkspaceChangeProposal:
        with self._write_session() as session:
            if session.get(ConversationRow, conversation_id) is None:
                raise NotFoundError("conversation", conversation_id)
            row = WorkspacePatchRow(
                proposal_id=_id(),
                conversation_id=conversation_id,
                workspace_hash=workspace_hash,
                proposal_hash=proposal_hash,
                edits_json=canonical_json(edits),
                diff_text=diff,
                citations_json=canonical_json(citations),
                expires_at=datetime.now(UTC) + timedelta(seconds=max(60, min(ttl_seconds, 3600))),
            )
            session.add(row)
            session.flush()
            return self._proposal(row)

    def get_workspace_patch(
        self, conversation_id: str, proposal_id: str
    ) -> WorkspaceChangeProposal:
        with self._read_session() as session:
            row = session.get(WorkspacePatchRow, proposal_id)
            if row is None or row.conversation_id != conversation_id:
                raise NotFoundError("workspace_patch", proposal_id)
            return self._proposal(row)

    def set_workspace_patch_applied(
        self, conversation_id: str, proposal_id: str, *, backup_dir: str, postimage: dict[str, str]
    ) -> WorkspaceChangeProposal:
        with self._write_session() as session:
            row = session.get(WorkspacePatchRow, proposal_id)
            if row is None or row.conversation_id != conversation_id:
                raise NotFoundError("workspace_patch", proposal_id)
            if row.status != "proposed" or _as_utc(row.expires_at) <= datetime.now(UTC):
                raise ConflictError(
                    "workspace_patch_not_applicable", "补丁不存在、已过期或已处理。"
                )
            row.status = "applied"
            row.applied_at = datetime.now(UTC)
            row.backup_dir = backup_dir
            row.postimage_json = canonical_json(postimage)
            session.flush()
            return self._proposal(row)

    def set_workspace_patch_undone(
        self, conversation_id: str, proposal_id: str
    ) -> WorkspaceChangeProposal:
        with self._write_session() as session:
            row = session.get(WorkspacePatchRow, proposal_id)
            if row is None or row.conversation_id != conversation_id:
                raise NotFoundError("workspace_patch", proposal_id)
            if row.status != "applied" or row.backup_dir is None:
                raise ConflictError("workspace_patch_not_undoable", "补丁当前不可撤销。")
            row.status = "undone"
            row.undone_at = datetime.now(UTC)
            session.flush()
            return self._proposal(row)

    def get_workspace_patch_metadata(
        self, conversation_id: str, proposal_id: str
    ) -> dict[str, Any] | None:
        with self._read_session() as session:
            row = session.get(WorkspacePatchRow, proposal_id)
            if row is None or row.conversation_id != conversation_id:
                raise NotFoundError("workspace_patch", proposal_id)
            if row.backup_dir is None:
                return None
            return {"backup_dir": row.backup_dir, "postimage": _load(row.postimage_json, {})}

    def invalidate_workspace_patch_undo(
        self, conversation_id: str, workspace_hash: str, except_proposal_id: str
    ) -> None:
        with self._write_session() as session:
            session.execute(
                update(WorkspacePatchRow)
                .where(
                    WorkspacePatchRow.conversation_id == conversation_id,
                    WorkspacePatchRow.workspace_hash == workspace_hash,
                    WorkspacePatchRow.proposal_id != except_proposal_id,
                    WorkspacePatchRow.status == "applied",
                )
                .values(status="superseded")
            )
