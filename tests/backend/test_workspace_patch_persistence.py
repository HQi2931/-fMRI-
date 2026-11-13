from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from neuroagent.application.contracts.conversations import (
    ConversationCreate,
    ConversationMode,
)
from neuroagent.application.errors import ConflictError, NotFoundError
from neuroagent.infrastructure.persistence.models import WorkspacePatchRow


def _conversation(service) -> str:
    return service.conversations.create_conversation(
        ConversationCreate(mode=ConversationMode.WORK, workspace_path="D:/workspace"),
        "workspace-patch-conversation",
    ).conversation_id


def _create_patch(
    repository,
    conversation_id: str,
    *,
    workspace_hash: str = "a" * 64,
    proposal_hash: str = "b" * 64,
):
    return repository.create_workspace_patch(
        conversation_id=conversation_id,
        workspace_hash=workspace_hash,
        proposal_hash=proposal_hash,
        edits=[
            {
                "path": "protocol.txt",
                "expected_sha256": "c" * 64,
                "start_line": 1,
                "end_line": 1,
                "replacement": "reviewed\n",
                "after_sha256": "d" * 64,
            }
        ],
        diff="-draft\n+reviewed\n",
        citations=[{"path": "protocol.txt", "locator": "line 1", "quote": "draft"}],
    )


def test_workspace_patch_persistence_tracks_review_apply_and_undo(service) -> None:
    conversation_id = _conversation(service)
    repository = service.repository
    proposal = _create_patch(repository, conversation_id)

    persisted = repository.get_workspace_patch(conversation_id, proposal.proposal_id)
    assert persisted == proposal
    assert persisted.status == "proposed"
    assert persisted.edits[0].path == "protocol.txt"
    assert persisted.citations[0].locator == "line 1"
    assert repository.get_workspace_patch_metadata(conversation_id, proposal.proposal_id) is None

    applied = repository.set_workspace_patch_applied(
        conversation_id,
        proposal.proposal_id,
        backup_dir="D:/backups/proposal-1",
        postimage={"protocol.txt": "e" * 64},
    )
    metadata = repository.get_workspace_patch_metadata(conversation_id, proposal.proposal_id)
    assert applied.status == "applied"
    assert applied.applied_at is not None
    assert metadata == {
        "backup_dir": "D:/backups/proposal-1",
        "postimage": {"protocol.txt": "e" * 64},
    }

    undone = repository.set_workspace_patch_undone(conversation_id, proposal.proposal_id)
    assert undone.status == "undone"
    assert undone.undone_at is not None
    with pytest.raises(ConflictError) as already_processed:
        repository.set_workspace_patch_applied(
            conversation_id,
            proposal.proposal_id,
            backup_dir="D:/backups/again",
            postimage={},
        )
    assert already_processed.value.code == "workspace_patch_not_applicable"
    with pytest.raises(ConflictError) as not_undoable:
        repository.set_workspace_patch_undone(conversation_id, proposal.proposal_id)
    assert not_undoable.value.code == "workspace_patch_not_undoable"


def test_workspace_patch_persistence_invalidates_older_undo_and_checks_scope(service) -> None:
    conversation_id = _conversation(service)
    other_conversation_id = service.conversations.create_conversation(
        ConversationCreate(mode=ConversationMode.WORK, workspace_path="D:/workspace"),
        "other-workspace-patch-conversation",
    ).conversation_id
    repository = service.repository
    first = _create_patch(repository, conversation_id)
    second = _create_patch(repository, conversation_id, proposal_hash="f" * 64)
    repository.set_workspace_patch_applied(
        conversation_id, first.proposal_id, backup_dir="D:/backups/first", postimage={}
    )
    repository.set_workspace_patch_applied(
        conversation_id, second.proposal_id, backup_dir="D:/backups/second", postimage={}
    )

    repository.invalidate_workspace_patch_undo(
        conversation_id, "a" * 64, except_proposal_id=second.proposal_id
    )

    assert repository.get_workspace_patch(conversation_id, first.proposal_id).status == "superseded"
    assert repository.get_workspace_patch(conversation_id, second.proposal_id).status == "applied"
    with pytest.raises(NotFoundError):
        repository.get_workspace_patch(other_conversation_id, first.proposal_id)
    with pytest.raises(NotFoundError):
        repository.get_workspace_patch(conversation_id, "missing-proposal")
    with pytest.raises(NotFoundError):
        repository.create_workspace_patch(
            conversation_id="missing-conversation",
            workspace_hash="a" * 64,
            proposal_hash="b" * 64,
            edits=[],
            diff="",
            citations=[],
        )

    with repository._write_session() as session:
        row = session.get(WorkspacePatchRow, first.proposal_id)
        assert row is not None and row.status == "superseded"


def test_expired_workspace_patch_cannot_be_applied(service) -> None:
    conversation_id = _conversation(service)
    repository = service.repository
    proposal = _create_patch(repository, conversation_id)
    with repository._write_session() as session:
        row = session.get(WorkspacePatchRow, proposal.proposal_id)
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises(ConflictError) as expired:
        repository.set_workspace_patch_applied(
            conversation_id,
            proposal.proposal_id,
            backup_dir="D:/backups/expired",
            postimage={},
        )

    assert expired.value.code == "workspace_patch_not_applicable"
