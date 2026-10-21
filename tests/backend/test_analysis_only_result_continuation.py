from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from neuroagent.api.app import create_app
from neuroagent.application.automation.models import TaskStatus
from neuroagent.application.errors import ConflictError
from neuroagent.application.reporting import build_statistical_reproducibility_report
from neuroagent.domain.fmri import (
    FdrCorrection,
    StatisticalDesignRevision,
    StatisticalResultManifest,
)
from neuroagent.infrastructure.persistence.models import ArtifactRow
from tests.backend.test_automation_service import _real_zero_result, _statistical_design
from tests.backend.test_work_dialogue import generated
from tests.backend.test_work_task_bindings import _external_analysis_target


def _ready(service, source_root, work_root, monkeypatch):
    goal, role_task, run, payload = _external_analysis_target(
        service, source_root, work_root, monkeypatch
    )
    plan = service.plans.get_plan(run.plan_revision_id)
    design = StatisticalDesignRevision.model_validate(plan.plan["design"])
    _summary, detail, artifacts = _real_zero_result(plan=plan, design=_statistical_design())
    manifest = StatisticalResultManifest.model_validate(detail.manifest).model_copy(
        update={
            "run_id": run.run_id,
            "design_revision_id": design.revision_id,
            "correction": FdrCorrection.model_validate(plan.plan["correction"]),
        }
    )
    report = build_statistical_reproducibility_report(
        manifest=manifest,
        design=design,
        correction=manifest.correction,
        qc_review_hash=design.qc_review_hash,
        environment_hash=plan.environment_hash,
        plan_hash=plan.plan_hash,
    )
    with service.database.session_factory.begin() as session:
        for artifact in artifacts:
            session.add(
                ArtifactRow(
                    artifact_id=artifact.artifact_id,
                    project_id=goal.project_id,
                    run_id=run.run_id,
                    artifact_type=artifact.artifact_type,
                    relative_path=artifact.relative_path,
                    checksum=artifact.checksum,
                    size_bytes=artifact.size_bytes,
                    provenance_json=json.dumps(artifact.provenance),
                )
            )
        session.execute(
            text(
                "UPDATE workflow_runs SET state='succeeded', attempt=1, version=version+1 "
                "WHERE run_id=:run_id"
            ),
            {"run_id": run.run_id},
        )
    service.repository.create_statistical_result(
        project_id=goal.project_id,
        run_id=run.run_id,
        design_revision_id=design.revision_id,
        mode="real",
        non_scientific=False,
        non_scientific_reason=None,
        bundle_hash=report.bundle_hash,
        manifest=manifest.model_dump(mode="json"),
        report_markdown=report.markdown,
        report_json=report.json_text,
        actor="fixture",
    )
    return goal, role_task, run, payload


def test_external_result_finalization_binds_root_atomically_and_progress_api(
    service, source_root, work_root, monkeypatch
):
    goal, role_task, _run, _payload = _ready(service, source_root, work_root, monkeypatch)
    before = service.automation.get_composite_goal_model_budget(goal.intent_task_id)
    outcome = service.automation.reconcile_waiting_run(role_task.work_task_id)
    assert outcome.task.status is TaskStatus.COMPLETED
    root = service.automation.get_composite_goal(goal.conversation_id, goal.intent_task_id)
    assert root.preprocessing_task_id is None
    assert root.analysis_proposal_outbox_intent_task_id is None
    assert root.result_interpretation_outbox_task_id == role_task.work_task_id
    outbox = service.automation.get_result_interpretation_outbox(role_task.work_task_id)
    verified = service.automation.verify_result_interpretation_source(outbox)
    assert verified.model_facts.significant_voxel_count == 0
    assert service.automation.get_composite_goal_model_budget(goal.intent_task_id) == before
    progress = service.automation.get_composite_goal_progress(
        goal.conversation_id, goal.intent_task_id
    )
    assert progress.status.value == "waiting_result_interpretation"
    app = create_app(service.settings)
    try:
        with TestClient(app) as client:
            response = client.get(f"/api/v1/conversations/{goal.conversation_id}/composite-goals")
        assert response.status_code == 200
        item = next(
            item for item in response.json() if item["intent_task_id"] == goal.intent_task_id
        )
        assert item["goal_kind"] == "analysis_only_external_import"
        assert item["preprocessing_task_id"] is None
        assert item["analysis_summary"] == "请用所选外部指标进行独立统计分析。"
    finally:
        app.state.service.close()
    assert service.automation.reconcile_waiting_run(role_task.work_task_id).task == outcome.task
    assert len(service.runs.list_runs(project_id=goal.project_id)) == 1


@pytest.mark.parametrize("mutation", [None, "source", "plan_source", "active_attempt"])
def test_analysis_only_progress_api_exposes_only_verified_observable_run(
    service, source_root, work_root, monkeypatch, mutation
):
    goal, _role_task, run, _payload = _ready(service, source_root, work_root, monkeypatch)
    plan = service.plans.get_plan(run.plan_revision_id) if mutation == "plan_source" else None
    with service.database.engine.begin() as connection:
        connection.execute(
            text("UPDATE workflow_runs SET state='queued' WHERE run_id=:run_id"),
            {"run_id": run.run_id},
        )
        if mutation == "source":
            connection.execute(
                text("UPDATE external_analysis_imports SET version=version+1 WHERE import_id=:id"),
                {"id": goal.source_external_import_id},
            )
        elif mutation == "plan_source":
            assert plan is not None
            content = dict(plan.plan)
            content["design"] = {**content["design"], "external_import_id": "wrong-source"}
            connection.execute(
                text("UPDATE plan_revisions SET plan_json=:content WHERE plan_revision_id=:id"),
                {"id": plan.plan_revision_id, "content": json.dumps(content)},
            )
        elif mutation == "active_attempt":
            connection.execute(
                text(
                    "UPDATE composite_goal_links SET analysis_work_task_id=NULL "
                    "WHERE intent_task_id=:id"
                ),
                {"id": goal.intent_task_id},
            )

    with TestClient(create_app(service=service)) as client:
        response = client.get(f"/api/v1/conversations/{goal.conversation_id}/composite-goals")
    assert response.status_code == 200, response.text
    item = next(item for item in response.json() if item["intent_task_id"] == goal.intent_task_id)
    assert item["preprocessing_run_id"] is None
    if mutation is None:
        assert item["analysis_run_id"] == run.run_id
    else:
        assert item["analysis_run_id"] is None


@pytest.mark.parametrize("mutation", ["source", "plan_source", "active_attempt"])
def test_external_result_binding_rejects_changed_authority_atomically(
    service, source_root, work_root, monkeypatch, mutation
):
    goal, role_task, _run, _payload = _ready(service, source_root, work_root, monkeypatch)
    before = service.automation.get_task(role_task.work_task_id)
    with service.database.engine.begin() as connection:
        if mutation == "source":
            connection.execute(
                text("UPDATE external_analysis_imports SET version=version+1 WHERE import_id=:id"),
                {"id": goal.source_external_import_id},
            )
        elif mutation == "plan_source":
            plan = service.plans.get_plan(before.authority_refs.plan_revision_id)
            content = dict(plan.plan)
            content["design"] = {**content["design"], "external_import_id": "wrong-source"}
            connection.execute(
                text("UPDATE plan_revisions SET plan_json=:content WHERE plan_revision_id=:id"),
                {"id": plan.plan_revision_id, "content": json.dumps(content)},
            )
        else:
            connection.execute(
                text(
                    "UPDATE composite_goal_links SET analysis_work_task_id=NULL "
                    "WHERE intent_task_id=:id"
                ),
                {"id": goal.intent_task_id},
            )
    try:
        result = service.automation.reconcile_waiting_run(before.task_id)
    except ConflictError:
        after = service.automation.get_task(before.task_id)
        assert after.status == before.status
        assert after.steps == before.steps
    else:
        assert result.task.status is TaskStatus.BLOCKED
    with service.database.engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM statistical_result_interpretation_outbox")
            ).scalar()
            == 0
        )
        assert (
            connection.execute(
                text(
                    "SELECT result_interpretation_outbox_task_id FROM composite_goal_links "
                    "WHERE intent_task_id=:id"
                ),
                {"id": goal.intent_task_id},
            ).scalar()
            is None
        )


def test_analysis_only_result_migration_preserves_ledgers_and_rejects_lossy_downgrade(
    service, source_root, work_root, monkeypatch
):
    _goal, role_task, _run, _payload = _ready(service, source_root, work_root, monkeypatch)
    tables = (
        "composite_goal_links",
        "composite_goal_analysis_attempts",
        "composite_goal_model_budgets",
        "composite_goal_model_call_reservations",
    )

    def ledger():
        with service.database.engine.connect() as connection:
            return {
                table: connection.execute(text(f"SELECT * FROM {table}")).all() for table in tables
            }

    before = ledger()
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", service.settings.database_url)
    command.downgrade(config, "0033_analysis_only_composite_goals")
    assert ledger() == before
    command.upgrade(config, "head")
    assert ledger() == before
    service.automation.reconcile_waiting_run(role_task.work_task_id)
    linked = ledger()
    with pytest.raises(RuntimeError, match="Cannot downgrade 0034"):
        command.downgrade(config, "0033_analysis_only_composite_goals")
    assert ledger() == linked
    with service.database.engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []


@pytest.mark.parametrize("mutation", [None, "intent", "result_version"])
def test_external_result_consumer_resumes_budget_and_checks_assistant_continuation(
    service, source_root, work_root, monkeypatch, mutation
):
    goal, role_task, _run, _payload = _ready(service, source_root, work_root, monkeypatch)
    service.automation.reconcile_waiting_run(role_task.work_task_id)
    before = service.automation.get_composite_goal_model_budget(goal.intent_task_id)
    provider = AsyncMock(
        return_value=generated(
            "conversation", answer="No significant voxels; this does not establish equivalence."
        )
    )
    monkeypatch.setattr(service.models, "_generate_rsfmri_chat", provider)
    conversation_before = service.conversations.get_conversation(goal.conversation_id)
    result = asyncio.run(
        service.conversations.consume_next_result_interpretation_outbox(
            "external-result-worker", lease_seconds=30
        )
    )
    assert result == "completed"
    provider.assert_awaited_once()
    after = service.automation.get_composite_goal_model_budget(goal.intent_task_id)
    assert after.used_model_calls == before.used_model_calls + 1
    assert after.used_tokens > before.used_tokens
    conversation = service.conversations.get_conversation(goal.conversation_id)
    assert len(conversation.messages) == len(conversation_before.messages) + 1
    message = conversation.messages[-1]
    assert message.role.value == "assistant"
    assert message.payload["analysis_intent_envelope"]["task_id"] == goal.intent_task_id
    assert message.payload["statistical_result_interpretation"]["status"] == "completed"
    assert (
        asyncio.run(
            service.conversations.consume_next_result_interpretation_outbox(
                "external-result-worker", lease_seconds=30
            )
        )
        is None
    )
    if mutation is not None:
        payload = dict(message.payload)
        if mutation == "intent":
            payload["analysis_intent_envelope"] = {
                **payload["analysis_intent_envelope"],
                "task_id": "wrong-intent",
            }
        else:
            payload["statistical_result_interpretation"] = {
                **payload["statistical_result_interpretation"],
                "result_version": 999,
            }
        with service.database.engine.begin() as connection:
            connection.execute(
                text("UPDATE conversation_messages SET payload_json=:payload WHERE message_id=:id"),
                {"id": message.message_id, "payload": json.dumps(payload)},
            )
    progress = service.automation.get_composite_goal_progress(
        goal.conversation_id, goal.intent_task_id
    )
    assert progress.status.value == ("completed" if mutation is None else "indeterminate")
    if mutation is not None:
        assert progress.reason_code == "result_continuation_mismatch"
        # Repository completion uses the same exact envelope guard inside the fenced commit.
        with service.database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE statistical_result_interpretation_outbox SET status='ready', "
                    "continuation_message_id=NULL WHERE task_id=:id"
                ),
                {"id": role_task.work_task_id},
            )
        claim = service.automation.claim_next_result_interpretation_outbox(
            "mismatch-worker", lease_seconds=30
        )
        assert claim is not None
        with pytest.raises(ConflictError, match="continuation"):
            from neuroagent.application.automation.result_interpretation import (
                ResultInterpretationStatus,
            )

            service.automation.complete_result_interpretation_outbox(
                claim,
                status=ResultInterpretationStatus.COMPLETED,
                continuation_message_id=message.message_id,
            )
        assert (
            service.automation.get_result_interpretation_outbox(role_task.work_task_id).status.value
            == "ready"
        )


def test_external_result_consumer_exhausted_cumulative_budget_does_not_reset(
    service, source_root, work_root, monkeypatch
):
    goal, role_task, _run, _payload = _ready(service, source_root, work_root, monkeypatch)
    service.automation.reconcile_waiting_run(role_task.work_task_id)
    with service.database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE composite_goal_model_budgets SET max_model_calls=used_model_calls "
                "WHERE intent_task_id=:id"
            ),
            {"id": goal.intent_task_id},
        )
    before = service.automation.get_composite_goal_model_budget(goal.intent_task_id)
    provider = AsyncMock()
    monkeypatch.setattr(service.models, "_generate_rsfmri_chat", provider)
    assert (
        asyncio.run(
            service.conversations.consume_next_result_interpretation_outbox(
                "exhausted-result-worker", lease_seconds=30
            )
        )
        == "waiting_user"
    )
    provider.assert_not_awaited()
    after = service.automation.get_composite_goal_model_budget(goal.intent_task_id)
    assert after.used_model_calls == before.used_model_calls
    assert after.used_tokens == before.used_tokens
    outbox = service.automation.get_result_interpretation_outbox(role_task.work_task_id)
    assert outbox.last_error_code == "composite_goal_model_budget_exhausted"


@pytest.mark.parametrize("mutation", ["source", "result", "plan"])
def test_external_result_progress_and_consumer_reject_authority_drift(
    service, source_root, work_root, monkeypatch, mutation
):
    goal, role_task, _run, _payload = _ready(service, source_root, work_root, monkeypatch)
    service.automation.reconcile_waiting_run(role_task.work_task_id)
    with service.database.engine.begin() as connection:
        if mutation == "source":
            connection.execute(
                text("UPDATE external_analysis_imports SET version=version+1 WHERE import_id=:id"),
                {"id": goal.source_external_import_id},
            )
        elif mutation == "result":
            connection.execute(text("UPDATE statistical_results SET version=version+1"))
        else:
            connection.execute(
                text("UPDATE plan_revisions SET plan_hash=:hash WHERE plan_revision_id=:id"),
                {"id": _run.plan_revision_id, "hash": "f" * 64},
            )
    progress = service.automation.get_composite_goal_progress(
        goal.conversation_id, goal.intent_task_id
    )
    assert progress.status.value in {"blocked", "indeterminate"}
    outbox = service.automation.get_result_interpretation_outbox(role_task.work_task_id)
    with pytest.raises(ConflictError):
        service.automation.verify_result_interpretation_source(outbox)


def test_analysis_only_goal_without_proposal_waits_for_user_and_changed_source_blocks(
    service, source_root, work_root
):
    from tests.backend.test_work_task_bindings import _proposal

    conversation_id, request, payload = _proposal(service, source_root, work_root)
    imported = service.external_imports.get_external_analysis_import(
        payload["statistical_design_proposal"]["design"]["external_import_id"]
    )
    goal = service.automation.ensure_analysis_only_external_goal(
        conversation_id=conversation_id,
        project_id=request.project_id,
        analysis_request="Review required scientific choices",
        imported=imported,
    )
    progress = service.automation.get_composite_goal_progress(conversation_id, goal.intent_task_id)
    assert progress.status.value == "waiting_user"
    assert progress.reason_code == "analysis_draft_user_decision"
    with service.database.engine.begin() as connection:
        connection.execute(
            text("UPDATE external_analysis_imports SET version=version+1 WHERE import_id=:id"),
            {"id": imported.import_id},
        )
    progress = service.automation.get_composite_goal_progress(conversation_id, goal.intent_task_id)
    assert progress.status.value == "blocked"
    assert progress.reason_code == "analysis_only_source_changed"
