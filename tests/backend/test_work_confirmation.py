"""Public UI confirmation regressions using real compilation and queueing only."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from neuroagent.api import create_app
from neuroagent.application.contracts import WorkDraftUpdate
from neuroagent.bootstrap import build_service
from tests.backend.test_environment import _fake_stack
from tests.backend.test_science_agent_api import _minimal_preprocessing_payload
from tests.backend.test_work_dialogue import configure, create, generated, turn


@pytest.fixture
def reviewed_work(tmp_path, monkeypatch):
    source, work = tmp_path / "source", tmp_path / "work"
    subject = source / "FunRaw" / "sub-01"
    subject.mkdir(parents=True)
    work.mkdir()
    (subject / "rest.nii").write_bytes((348).to_bytes(4, "little") + b"\0" * 344)
    settings = _fake_stack(tmp_path / "fake", "preprocessing").model_copy(
        update={
            "database_url": f"sqlite:///{(tmp_path / 'dialogue.sqlite').as_posix()}",
            "allowed_source_roots": [source],
            "allowed_work_root": work,
            "enable_real_execution": True,
            "rag_db_dir": None,
        }
    )
    service = build_service(settings)
    values = _minimal_preprocessing_payload()
    values.pop("provenance")
    patch = {
        "preprocessing": values,
        "requested_metrics": ["alff"],
        "primary_outputs": ["raw_alff"],
        "study_protocol_ref": "test:reviewed-protocol",
        "qc_mode": "skip",
        "alff_falff": {
            "tr_seconds": 2.0,
            "frequency_band": {"low_hz": 0.01, "high_hz": 0.08},
            "requested_metrics": ["alff"],
            "requested_scalings": ["raw"],
            "mask_source": "dpabi_auto",
            "mask_artifact_id": None,
            "filter_timing": "disabled",
            "result_smoothing": False,
            "result_smoothing_fwhm_mm": None,
        },
    }
    model = configure(service, monkeypatch, [generated("prepare_preprocessing_plan", patch)])
    try:
        with TestClient(create_app(service=service)) as client:
            session = create(client, source)
            turn(
                client,
                session,
                "register workspace",
                "setup",
                action="setup_workspace",
                workspace_setup={
                    "project_name": "test project",
                    "dataset_name": "test input",
                    "work_root": str(work / "project"),
                    "selected_input_stage": "FunRaw",
                },
            )
            review = turn(client, session, "prepare ALFF", "review")
            payload = review["assistant_message"]["payload"]
            assert payload["plan_revision"]["state"] == "awaiting_approval"
            plan = service.repository.get_plan(payload["plan_revision"]["plan_revision_id"])
            frozen = service.repository.get_manifest_content(plan.plan["dataset_manifest_id"])
            inspected = service._inspect_source(
                source, report_only=False, selected_input_stage="FunRaw"
            )
            assert frozen == inspected, {
                k: (frozen.get(k), inspected.get(k))
                for k in frozen.keys() | inspected.keys()
                if frozen.get(k) != inspected.get(k)
            }
            assert not service._plan_staleness_reasons(
                plan,
                current_environment_hash=service.environment_provider.current().snapshot.environment_hash,
            )
            model.reset_mock()
            yield client, service, session, payload, model
    finally:
        service.close()


def confirm(client, session, payload, key="start", **overrides):
    body = {
        "command": payload["confirmation_request"]["options"][0]["command"],
        "expected_draft_hash": payload["work_draft"]["hash"],
        **overrides,
    }
    return client.post(
        f"/api/v1/conversations/{session}/work-confirmation",
        json=body,
        headers={"Idempotency-Key": "ui-confirm-" + key},
    )


def test_direct_confirmation_queues_once_and_recovers_response_after_later_edit(reviewed_work):
    client, service, session, payload, model = reviewed_work
    first = confirm(client, session, payload)
    assert first.status_code == 200, first.json().get("error")
    run_id = first.json()["conversation"]["active_run_id"]
    assert run_id
    assert len(service.repository.list_runs()) == 1
    assert (
        service.repository.get_plan(payload["plan_revision"]["plan_revision_id"]).state
        == "approved"
    )
    saved = first.json()["assistant_message"]["payload"]["work_draft"]
    service.update_work_draft(
        session,
        WorkDraftUpdate(base_hash=saved["hash"], patch={"qc_mode": "head_motion"}),
        "edit-after-start",
    )
    replay = confirm(client, session, payload)
    assert replay.status_code == 200, replay.json().get("error")
    assert replay.json() == first.json()
    assert len(service.repository.list_runs()) == 1
    model.assert_not_called()


def test_old_draft_and_tampered_command_cannot_approve_or_queue(reviewed_work):
    client, service, session, payload, model = reviewed_work
    stale = confirm(client, session, payload, expected_draft_hash="a" * 64)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "work_draft_changed"
    tampered = confirm(client, session, payload, key="tampered", command="start now")
    assert tampered.status_code == 409
    assert service.repository.list_runs() == []
    assert (
        service.repository.get_plan(payload["plan_revision"]["plan_revision_id"]).state
        == "awaiting_approval"
    )
    model.assert_not_called()


def test_second_click_with_new_key_does_not_create_another_run(reviewed_work):
    client, service, session, payload, model = reviewed_work
    first = confirm(client, session, payload)
    assert first.status_code == 200, first.json().get("error")
    second = confirm(client, session, payload, key="second-click")
    assert second.status_code == 200, second.json().get("error")
    assert (
        second.json()["conversation"]["active_run_id"]
        == first.json()["conversation"]["active_run_id"]
    )
    assert len(service.repository.list_runs()) == 1
    model.assert_not_called()


def test_confirmation_transaction_rolls_back_queue_approval_and_consumption(
    reviewed_work, monkeypatch
):
    client, service, session, payload, model = reviewed_work
    original = service.repository.append_conversation_exchange
    monkeypatch.setattr(
        service.repository,
        "append_conversation_exchange",
        Mock(side_effect=RuntimeError("simulated persistence failure")),
    )
    with pytest.raises(RuntimeError, match="simulated persistence failure"):
        confirm(client, session, payload)
    assert service.repository.list_runs() == []
    assert (
        service.repository.get_plan(payload["plan_revision"]["plan_revision_id"]).state
        == "awaiting_approval"
    )
    monkeypatch.setattr(service.repository, "append_conversation_exchange", original)
    retry = confirm(client, session, payload)
    assert retry.status_code == 200, retry.json().get("error")
    assert len(service.repository.list_runs()) == 1
    model.assert_not_called()
