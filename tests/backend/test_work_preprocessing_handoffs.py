from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from neuroagent.application.contracts.conversations import (
    ConversationCreate,
    ConversationTurnCreate,
)
from neuroagent.application.errors import InputValidationError
from neuroagent.application.work_agents import latest_handoff_for_agent, work_agent_role_for_turn
from neuroagent.application.work_handoffs import require_same_preprocessing_handoff
from neuroagent.context.interfaces import ContextPacket
from neuroagent.multi_agent.registry import WorkAgentRole

from .test_work_analysis_handoffs import _source
from .test_work_task_bindings import _proposal


def _append(service, conversation, proof):
    service.repository.append_conversation_exchange(
        conversation.conversation_id,
        user_content="Request metrics",
        assistant_content="Requirements",
        assistant_payload={
            "agent_role": "analysis",
            "agent_handoff": proof.model_dump(mode="json"),
        },
        tool=None,
        workspace_path=None,
        preferred_profile_id=None,
        project_id=conversation.project_id,
        active_run_id=None,
    )
    return service.conversations.get_conversation(conversation.conversation_id)


def test_reverse_request_has_server_evidence_and_stable_identity(service, source_root, work_root):
    project, run, _, _, _, _ = _source(service, source_root, work_root)
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work", project_id=project.project_id), "reverse-conversation"
    )
    args = dict(
        conversation_id=conversation.conversation_id,
        project_id=project.project_id,
        run_id=run.run_id,
        required_outputs=("zReHo", "fALFF", "zReHo"),
    )
    proof = service.statistics.prepare_work_preprocessing_handoff(
        **args, reason="Need both outputs"
    )
    again = service.statistics.prepare_work_preprocessing_handoff(
        **{**args, "required_outputs": ("falff", "zreho")}, reason="Different model wording"
    )
    assert proof.handoff_id == again.handoff_id
    assert proof.required_outputs == ("fALFF", "zReHo")
    assert proof.data_references["metrics"] and proof.data_references["qc_decision"]
    assert proof.data_references["source_evidence_status"] == "ready"
    assert require_same_preprocessing_handoff(proof, again).status == "received"
    assert len(service.repository.list_runs(project_id=project.project_id)) == 1
    with pytest.raises(InputValidationError) as invalid:
        service.statistics.prepare_work_preprocessing_handoff(
            **{**args, "required_outputs": ("system('run')",)}, reason="Forged requirement"
        )
    assert invalid.value.code == "work_handoff_outputs_invalid"


def test_reverse_receiver_rejects_file_change_before_model_or_plan_mutation(
    service,
    source_root,
    work_root,
    monkeypatch,
):
    project, run, _, _, metrics, root = _source(service, source_root, work_root)
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work", project_id=project.project_id), "reverse-conversation"
    )
    proof = service.statistics.prepare_work_preprocessing_handoff(
        conversation_id=conversation.conversation_id,
        project_id=project.project_id,
        run_id=run.run_id,
        required_outputs=("zReHo",),
        reason="Need a new metric",
    )
    conversation = _append(service, conversation, proof)
    target = root / metrics[0].relative_path
    target.write_bytes(b"x" * target.stat().st_size)
    model = AsyncMock(side_effect=AssertionError("Stale handoff must not reach model"))
    monkeypatch.setattr(service.work_dialogue, "_interpret", model)
    response = asyncio.run(
        service.work_dialogue.respond(
            conversation,
            ConversationTurnCreate(content="继续"),
            ContextPacket(question="Continue", metadata={"token_budget": 12000, "selected": {}}),
            workspace_path=None,
            project_id=project.project_id,
            preferred_profile_id=None,
            idempotency_key="receive-stale",
            has_profiles=True,
        )
    )
    assert response.payload["agent_handoff"]["status"] == "stale"
    model.assert_not_called()
    assert "work_draft" not in response.payload
    assert len(service.repository.list_runs(project_id=project.project_id)) == 1


def test_reverse_handoff_limit_counts_unique_requests_and_cannot_be_replayed_around(
    service,
    source_root,
    work_root,
):
    project, run, _, _, _, _ = _source(service, source_root, work_root)
    service.settings = service.settings.model_copy(update={"work_max_handoffs": 1})
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work", project_id=project.project_id), "reverse-conversation"
    )
    args = dict(
        conversation_id=conversation.conversation_id,
        project_id=project.project_id,
        run_id=run.run_id,
        reason="Need metrics",
    )
    first = service.statistics.prepare_work_preprocessing_handoff(
        **args, required_outputs=("ReHo",)
    )
    conversation = _append(service, conversation, first)
    receipt = require_same_preprocessing_handoff(first, first)
    _append(service, conversation, receipt)
    assert (
        service.statistics.prepare_work_preprocessing_handoff(
            **args, required_outputs=("ReHo",)
        ).status
        == "pending"
    )
    second = service.statistics.prepare_work_preprocessing_handoff(
        **args, required_outputs=("fALFF",)
    )
    assert second.status == "blocked"
    assert second.blocking_items == ("work_handoff_limit_reached",)
    conversation = service.conversations.get_conversation(conversation.conversation_id)
    _append(service, conversation, second)
    assert (
        service.statistics.prepare_work_preprocessing_handoff(
            **args, required_outputs=("fALFF",)
        ).status
        == "blocked"
    )


def test_preprocessing_receives_summary_without_private_analysis_draft(
    service,
    source_root,
    work_root,
    monkeypatch,
):
    project, run, _, _, _, _ = _source(service, source_root, work_root)
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work", project_id=project.project_id), "reverse-conversation"
    )
    proof = service.statistics.prepare_work_preprocessing_handoff(
        conversation_id=conversation.conversation_id,
        project_id=project.project_id,
        run_id=run.run_id,
        required_outputs=("ReHo",),
        reason="Need ReHo",
    )
    conversation = _append(service, conversation, proof)
    model = AsyncMock(
        return_value=(json.dumps({"action": "conversation", "answer": "Discuss"}), {}, None)
    )
    monkeypatch.setattr(service.work_dialogue, "_interpret", model)
    response = asyncio.run(
        service.work_dialogue.respond(
            conversation,
            ConversationTurnCreate(content="继续"),
            ContextPacket(question="Continue", metadata={"token_budget": 12000, "selected": {}}),
            workspace_path=None,
            project_id=project.project_id,
            preferred_profile_id=None,
            idempotency_key="receive-ready",
            has_profiles=True,
        )
    )
    assert response.payload["agent_handoff"]["status"] == "received"
    context = model.call_args.args[0]
    refs = context["work_context"]["agent_handoff"]["data_references"]
    assert refs["metric_count"] > 0 and "metrics" not in refs
    assert "analysis_draft" not in context["work_context"]
    assert "statistical_design_proposal" not in response.payload
    assert (
        latest_handoff_for_agent(
            conversation.model_copy(update={"project_id": "other-project"}),
            WorkAgentRole.PREPROCESSING,
        )
        is None
    )
    assert (
        work_agent_role_for_turn(
            "继续",
            requested=None,
            conversation=conversation.model_copy(update={"project_id": "other-project"}),
        )
        is WorkAgentRole.ANALYSIS
    )


def test_external_metric_request_preserves_unknown_raw_input_and_rechecks_images(
    service,
    source_root,
    work_root,
):
    conversation_id, request, _ = _proposal(service, source_root, work_root)
    args = dict(
        conversation_id=conversation_id,
        project_id=request.project_id,
        external_import_id=request.design.external_import_id,
        required_outputs=("ReHo",),
        reason="Need another metric",
    )
    proof = service.statistics.prepare_work_preprocessing_handoff(**args)
    assert proof.data_references["input_selection_required"] is True
    assert proof.data_references["external_source_evidence"]["inspection"][
        "unknown_processing_fields"
    ]
    imported = service.repository.get_external_analysis_import(request.design.external_import_id)
    path = source_root / "metrics" / imported.inspection.images[0].relative_path
    path.write_bytes(b"x" * path.stat().st_size)
    blocked = service.statistics.prepare_work_preprocessing_handoff(**args)
    assert blocked.status == "blocked"
    assert blocked.required_outputs == proof.required_outputs
    assert blocked.data_references == proof.data_references
    assert blocked.blocking_items


def test_analysis_proposes_verified_requirements_without_changing_preprocessing_draft(
    service,
    source_root,
    work_root,
    monkeypatch,
):
    project, run, _, _, _, _ = _source(service, source_root, work_root)
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work", project_id=project.project_id), "reverse-proposal"
    )
    initial = {"hash": "b" * 64, "draft": {"target_outputs": ["zALFF"]}}
    service.repository.append_conversation_exchange(
        conversation.conversation_id,
        user_content="ALFF",
        assistant_content="Parameters",
        assistant_payload={"agent_role": "preprocessing", "work_draft": initial},
        tool=None,
        workspace_path=None,
        preferred_profile_id=None,
        project_id=project.project_id,
        active_run_id=run.run_id,
    )
    conversation = service.conversations.get_conversation(conversation.conversation_id)
    model = AsyncMock(
        return_value=(
            json.dumps(
                {
                    "action": "request_preprocessing_handoff",
                    "answer": "Need ReHo",
                    "required_preprocessing_outputs": ["zReHo"],
                }
            ),
            {},
            None,
        )
    )
    monkeypatch.setattr(service.work_dialogue, "_interpret", model)
    response = asyncio.run(
        service.work_dialogue.respond(
            conversation,
            ConversationTurnCreate(content="比较 ReHo", target_run_id=run.run_id),
            ContextPacket(question="Compare", metadata={"token_budget": 12000}),
            workspace_path=None,
            project_id=project.project_id,
            preferred_profile_id=None,
            idempotency_key="reverse-propose",
            has_profiles=True,
        )
    )
    assert response.payload["agent_role"] == "analysis"
    assert response.payload["agent_handoff"]["required_outputs"] == ["zReHo"]
    assert response.payload["agent_handoff"]["data_references"]["metrics"]
    assert (
        "work_draft" not in response.payload
        and "statistical_design_proposal" not in response.payload
    )
    assert (
        service.conversations.get_conversation(conversation.conversation_id)
        .messages[-1]
        .payload["work_draft"]
        == initial
    )
    assert len(service.repository.list_runs(project_id=project.project_id)) == 1
