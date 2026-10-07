"""Deterministic draft edits never invoke a provider or execute a job."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from neuroagent.api import create_app
from neuroagent.application.contracts import ConversationCreate
from neuroagent.application.work_draft import draft_hash
from tests.backend.test_work_draft import complete_draft


def seed(service, source_root, *, mode="work", revision=None):
    conversation = service.create_conversation(
        ConversationCreate(mode=mode, workspace_path=str(source_root)), "seed"
    )
    draft = complete_draft()
    service.repository.append_conversation_exchange(
        conversation.conversation_id,
        user_content="prepare",
        assistant_content="review",
        assistant_payload={
            "work_draft": {
                "draft": draft.model_dump(mode="json"),
                "hash": draft_hash(draft),
                "workspace_path": str(source_root),
                "compiled": True,
                "input_revision": revision,
            }
        },
        tool=None,
        workspace_path=str(source_root),
        preferred_profile_id=None,
        project_id=None,
        active_run_id=None,
    )
    return conversation.conversation_id, draft_hash(draft)


def test_changed_inputs_reject_edit_and_start_but_review_persists_recovery(service, source_root):
    report = service.conversation_work.workspace_stage_candidates(str(source_root))
    session, original = seed(
        service, source_root, revision=report["data_profile"]["metadata_revision"]
    )
    (source_root / "new-sidecar.txt").write_text("synthetic", encoding="utf-8")
    with TestClient(create_app(service=service)) as client:
        for suffix, method, body in [
            ("work-draft", client.patch, {"base_hash": original, "patch": {"qc_mode": "skip"}}),
            ("work-review", client.post, {"base_hash": original}),
            ("work-confirmation", client.post, {"command": "old", "expected_draft_hash": original}),
        ]:
            response = method(
                f"/api/v1/conversations/{session}/{suffix}",
                json=body,
                headers={"Idempotency-Key": "changed-" + suffix},
            )
            if suffix == "work-review":
                assert response.status_code == 200, response.text
                payload = response.json()["assistant_message"]["payload"]
                assert payload["work_draft"]["compiled"] is False
                assert payload["work_draft"]["input_manifest_stale"] is True
                assert payload["confirmation_request"]["title"] == "重新确认输入阶段"
                replay = method(
                    f"/api/v1/conversations/{session}/{suffix}",
                    json=body,
                    headers={"Idempotency-Key": "changed-" + suffix},
                )
                assert replay.json() == response.json()
                stored = service.get_conversation(session)
                assert stored.messages[-1].payload["work_draft"] == payload["work_draft"]
                assert service.repository.list_runs() == []
            else:
                assert response.status_code == 409, response.text
                assert response.json()["error"]["code"] == "work_input_changed"


def test_saved_edit_response_remains_replayable_after_input_change(service, source_root):
    report = service.conversation_work.workspace_stage_candidates(str(source_root))
    session, original = seed(
        service, source_root, revision=report["data_profile"]["metadata_revision"]
    )
    with TestClient(create_app(service=service)) as client:
        arguments = {
            "json": {"base_hash": original, "patch": {"preprocessing": {"dummy_scans": 2}}},
            "headers": {"Idempotency-Key": "revision-replay"},
        }
        first = client.patch(f"/api/v1/conversations/{session}/work-draft", **arguments)
        assert first.status_code == 200, first.text
        (source_root / "new-sidecar.txt").write_text("synthetic", encoding="utf-8")
        replay = client.patch(f"/api/v1/conversations/{session}/work-draft", **arguments)
        assert replay.json() == first.json()


def test_save_preserves_draft_and_is_idempotent(service, source_root, monkeypatch):
    provider = AsyncMock(side_effect=AssertionError("no provider"))
    monkeypatch.setattr(service, "_generate_rsfmri_chat", provider)
    session, original = seed(service, source_root)
    body = {"base_hash": original, "patch": {"preprocessing": {"dummy_scans": 2}}}
    with TestClient(create_app(service=service)) as client:
        response = client.patch(
            f"/api/v1/conversations/{session}/work-draft",
            json=body,
            headers={"Idempotency-Key": "draft-edit"},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        saved = data["assistant_message"]["payload"]["work_draft"]
        assert saved["draft"]["preprocessing"]["dummy_scans"] == 2
        assert (
            saved["draft"]["preprocessing"]["tr_seconds"]
            == complete_draft().preprocessing["tr_seconds"]
        )
        assert saved["compiled"] is False
        assert saved["sources"]["preprocessing.dummy_scans"] == "user"
        assert saved["hash"] != original
        replay = client.patch(
            f"/api/v1/conversations/{session}/work-draft",
            json=body,
            headers={"Idempotency-Key": "draft-edit"},
        )
        assert replay.json() == data
        conflict = client.patch(
            f"/api/v1/conversations/{session}/work-draft",
            json=body,
            headers={"Idempotency-Key": "old-version"},
        )
        assert conflict.status_code == 409
    provider.assert_not_called()


@pytest.mark.parametrize(
    "patch",
    [
        {"confirmed_hash": "a" * 64},
        {"preprocessing": {"provenance": []}},
        {"preprocessing": {"slice_timing": {"invented": 1}}},
        {"preprocessing": {"t1": {"invented": 1, "structural_artifact_id": ""}}},
        {"preprocessing": {"tr_seconds": "two"}},
    ],
)
def test_rejects_hidden_or_malformed_fields(service, source_root, patch):
    session, original = seed(service, source_root)
    with TestClient(create_app(service=service)) as client:
        response = client.patch(
            f"/api/v1/conversations/{session}/work-draft",
            json={"base_hash": original, "patch": patch},
            headers={"Idempotency-Key": "draft-invalid"},
        )
    assert response.status_code == 422, response.text


def test_invalid_science_saved_with_issues_and_no_confirmation(service, source_root):
    session, original = seed(service, source_root)
    with TestClient(create_app(service=service)) as client:
        response = client.patch(
            f"/api/v1/conversations/{session}/work-draft",
            json={"base_hash": original, "patch": {"preprocessing": {"tr_seconds": -1}}},
            headers={"Idempotency-Key": "draft-numeric"},
        )
    assert response.status_code == 200, response.text
    payload = response.json()["assistant_message"]["payload"]
    assert any("tr_seconds" in issue for issue in payload["draft_issues"])
    assert "confirmation_request" not in payload


def test_chat_cannot_edit_work_draft(service, source_root):
    session, original = seed(service, source_root, mode="chat")
    with TestClient(create_app(service=service)) as client:
        response = client.patch(
            f"/api/v1/conversations/{session}/work-draft",
            json={"base_hash": original, "patch": {}},
            headers={"Idempotency-Key": "chat-edit"},
        )
    assert response.status_code == 422


def test_t1_edit_has_no_extra_confirmation_or_review_step(service, source_root):
    session, original = seed(service, source_root)
    patch = {
        "preprocessing": {
            "normalization": {"mode": 3, "structural_artifact_id": "workspace:anatomical-source"},
            "t1": {
                "preparation": "reorient_crop",
                "coregister_to_functional": True,
                "segmentation": "new_segment",
                "affine_regularization": "mni",
                "dartel": True,
            },
        }
    }
    with TestClient(create_app(service=service)) as client:
        response = client.patch(
            f"/api/v1/conversations/{session}/work-draft",
            json={"base_hash": original, "patch": patch},
            headers={"Idempotency-Key": "t1-edit-no-review"},
        )
    assert response.status_code == 200, response.text
    payload = response.json()["assistant_message"]["payload"]
    t1 = payload["work_draft"]["draft"]["preprocessing"]["t1"]
    assert t1["structural_artifact_id"] == "workspace:anatomical-source"
    steps = payload["plan_preview"]["common_steps"]
    assert "prepare_t1" in {step["step_id"] for step in steps}
    assert "review_t1" not in {step["step_id"] for step in steps}
    assert "t1_preparation_review" not in payload
    assert payload["confirmation_request"]["title"] == "确认方案参数"
