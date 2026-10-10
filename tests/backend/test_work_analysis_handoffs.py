from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from neuroagent.application.contracts import (
    ApprovalCreate,
    ConversationCreate,
    ConversationTurnCreate,
    DatasetCreate,
    ManifestScanRequest,
    PlanRevisionCreate,
    PlanValidationRequest,
    QcReviewApprove,
    QcReviewCreate,
    RunCreate,
    StatisticalDesignCreate,
    StatisticalDesignValidationRequest,
    StatisticalWorkContext,
    StatisticsRunCreate,
)
from neuroagent.application.errors import ConflictError
from neuroagent.application.statistics_draft import StatisticsDraft, statistics_draft_hash
from neuroagent.application.work_handoffs import (
    handoff_reference_summary,
    require_same_handoff,
)
from neuroagent.bootstrap import build_worker
from neuroagent.context.interfaces import ContextPacket
from neuroagent.domain.fmri.qc import QcCheck, QcSeverity

from .conftest import make_bids_dataset, make_project
from .test_science_agent_api import _mask_lineage, _metric_lineage


def _source(
    service, source_root, work_root, *, approve=True, mixed_version=False, shared_masks=False
):
    project = make_project(service, source_root, work_root)
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Handoff source",
            source_path=str(make_bids_dataset(source_root)),
            expected_project_version=project.version,
        ),
        "handoff-dataset",
    )
    manifest = service.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "handoff-manifest",
    )
    plan = service.create_plan(
        PlanRevisionCreate(
            project_id=project.project_id,
            expected_project_version=service.get_project(project.project_id).version,
            plan={
                "skill_id": "test.mock",
                "steps": [{"capability": "mock.execute"}],
                "dataset_manifest_id": manifest.manifest_id,
            },
            manifest_hash=manifest.content_hash,
            environment_hash=service.environment_provider.current().snapshot.environment_hash,
        ),
        "handoff-source-plan",
    )
    plan = service.validate_plan(
        plan.plan_revision_id,
        PlanValidationRequest(expected_version=plan.version, issues=[]),
        "handoff-source-validation",
    )
    service.approve_plan(
        plan.plan_revision_id,
        ApprovalCreate(
            expected_version=plan.version,
            plan_hash=plan.plan_hash,
            actor="test",
            decision="approved",
            reason="Synthetic fixture",
        ),
        "handoff-source-approval",
    )
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
        ),
        "handoff-source-run",
    )
    assert build_worker(service).run_once()
    root = Path(project.work_root) / run.run_id

    def register(path, artifact_type, lineage, *, data=None, extra_provenance=None):
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        # Integrity fixtures are explicitly synthetic; no scientific NIfTI claim.
        file.write_bytes(path.encode() if data is None else data)
        service.repository.register_artifacts(
            project.project_id,
            run.run_id,
            (
                {
                    "artifact_type": artifact_type,
                    "relative_path": path,
                    "checksum": hashlib.sha256(file.read_bytes()).hexdigest(),
                    "size_bytes": file.stat().st_size,
                    "provenance": {
                        "synthetic": True,
                        "executor": "test_fixture",
                        "lineage": lineage.model_dump(mode="json"),
                        **(extra_provenance or {}),
                    },
                },
            ),
        )
        return next(
            item for item in service.list_artifacts(run.run_id) if item.relative_path == path
        )

    subjects = tuple(entry.subject_id for entry in manifest.subjects)
    source_masks = []
    if shared_masks:
        for subject in subjects:
            source_masks.append(
                register(
                    f"output/{subject}/brain_mask.nii",
                    "preprocessing.subject_brain_mask",
                    _mask_lineage(manifest.content_hash).model_copy(update={"subject_id": subject}),
                    data=b"identical synthetic mask evidence",
                )
            )
    shared = (
        {
            "shared_effective_mask": {
                "method": "identical_effective_masks",
                "source_masks": [
                    {
                        "subject_id": item.provenance["lineage"]["subject_id"],
                        "relative_path": item.relative_path,
                        "checksum": item.checksum,
                        "size_bytes": item.size_bytes,
                    }
                    for item in source_masks
                ],
            }
        }
        if shared_masks
        else {}
    )
    mask = register(
        "output/mask.nii",
        "preprocessing.brain_mask",
        _mask_lineage(manifest.content_hash),
        data=b"identical synthetic mask evidence" if shared_masks else None,
        extra_provenance=shared,
    )
    metrics = []
    for subject in reversed(subjects):
        lineage = _metric_lineage(manifest.content_hash, subject, mask.artifact_id)
        if mixed_version and subject == subjects[0]:
            lineage = lineage.model_copy(update={"producer_step_hash": "e" * 64})
        metrics.append(register(f"output/{subject}.nii", "metric.synthetic.alff", lineage))
    run = service.get_run(run.run_id)
    review = service.create_qc_review(
        QcReviewCreate(
            run_id=run.run_id,
            expected_run_version=run.version,
            metric_artifact_ids=tuple(item.artifact_id for item in metrics),
            included_subject_ids=subjects,
            excluded_subject_ids=(),
            exclusion_reasons=(),
            checks=(
                QcCheck(
                    code="MASK_GRID_MATCH",
                    severity=QcSeverity.BLOCKING,
                    passed=True,
                    evidence_artifact_ids=(mask.artifact_id,),
                    message="Synthetic fixture",
                ),
            ),
        ),
        "handoff-qc-review",
    )
    if approve:
        service.approve_qc_review(
            review.review.review_revision_id,
            QcReviewApprove(
                expected_review_version=review.version,
                expected_run_version=run.version,
                review_hash=review.review.content_hash,
                actor="test",
                approved=True,
                reason="Synthetic fixture review",
            ),
            "handoff-qc-approve",
        )
    return project, run, manifest, subjects, metrics, root


def _work_plan(service, project, run, subjects):
    draft = StatisticsDraft(
        test="one_sample_t",
        metric_artifact_type="metric.synthetic.alff",
        tail="two_sided",
        missing_value_policy="error",
        correction_method="fdr",
        correction_parameters={"q_threshold": 0.05},
        one_sample_baseline=0,
    )
    proposal, issues = service.prepare_statistical_design_proposal(run_id=run.run_id, draft=draft)
    assert proposal is not None and not issues, issues
    assert proposal.design.subject_order == subjects
    proof = service.prepare_work_analysis_handoff(run.run_id, project.project_id)
    assert proof.status == "ready"
    conversation = service.create_conversation(
        ConversationCreate(mode="work", project_id=project.project_id),
        "handoff-conversation",
    )
    service.repository.append_conversation_exchange(
        conversation.conversation_id,
        user_content="Analyse",
        assistant_content="Review",
        assistant_payload={
            "agent_role": "analysis",
            "analysis_draft": {
                "draft": draft.model_dump(mode="json"),
                "hash": statistics_draft_hash(draft),
            },
            "statistical_design_proposal": proposal.model_dump(mode="json"),
            "agent_handoff": require_same_handoff(proof, proof).model_dump(mode="json"),
        },
        tool=None,
        workspace_path=None,
        preferred_profile_id=None,
        project_id=project.project_id,
        active_run_id=None,
    )
    created = service.create_statistical_design(
        StatisticalDesignCreate(
            project_id=project.project_id,
            expected_project_version=proposal.project_version,
            input_manifest_hash=proposal.input_manifest_hash,
            design=proposal.design,
            correction=proposal.correction,
            work_context=StatisticalWorkContext(
                conversation_id=conversation.conversation_id, proposal_hash=proposal.proposal_hash
            ),
        ),
        "handoff-statistical-plan",
    )
    return created.plan_revision, proof


def test_full_handoff_is_deterministic_and_preserves_qc_order(service, source_root, work_root):
    project, run, manifest, subjects, metrics, _ = _source(service, source_root, work_root)
    proof = service.prepare_work_analysis_handoff(run.run_id, project.project_id)
    again = service.prepare_work_analysis_handoff(run.run_id, project.project_id)
    assert proof == again and proof.status == "ready"
    refs = proof.data_references
    assert refs["dataset_id"] == manifest.dataset_id
    assert refs["dataset_manifest_hash"] == manifest.content_hash
    assert refs["included_subject_ids"] == list(subjects)
    assert [item["lineage"]["subject_id"] for item in refs["metrics"]] == list(subjects)
    assert {item["artifact_id"] for item in refs["metrics"]} == {
        item.artifact_id for item in metrics
    }
    assert refs["masks"][0]["checksum"] and refs["qc_decision"]["qc_review_hash"]
    assert all(
        item["metric_version"] and item["lineage_hash"] and item["provenance"]["synthetic"]
        for item in refs["metrics"]
    )
    assert require_same_handoff(proof, again).status == "received"
    summary = handoff_reference_summary(refs)
    assert summary["subject_count"] == len(subjects)
    assert "metrics" not in summary and "lineage" not in json.dumps(summary)
    with pytest.raises(ConflictError) as mismatch:
        service.prepare_work_analysis_handoff(run.run_id, "another-project")
    assert mismatch.value.code == "work_handoff_project_mismatch"


def test_shared_effective_mask_preserves_and_rechecks_each_subject_source(
    service, source_root, work_root
):
    project, run, _, subjects, _, root = _source(service, source_root, work_root, shared_masks=True)
    proof = service.prepare_work_analysis_handoff(run.run_id, project.project_id)
    assert proof.status == "ready"
    masks = proof.data_references["masks"]
    assert len(masks) == len(subjects) + 1
    assert {item["lineage"]["subject_id"] for item in masks} == {None, *subjects}
    assert len({item["checksum"] for item in masks}) == 1
    plan, _ = _work_plan(service, project, run, subjects)
    source = next(item for item in masks if item["lineage"]["subject_id"] == subjects[0])
    path = root / source["relative_path"]
    path.write_bytes(b"x" * path.stat().st_size)
    current = service.prepare_work_analysis_handoff(run.run_id, project.project_id)
    assert current.status == "blocked"
    with pytest.raises(ConflictError, match="changed or is not ready"):
        require_same_handoff(proof, current)
    with pytest.raises(ConflictError):
        service.validate_statistical_design(
            plan.plan_revision_id,
            StatisticalDesignValidationRequest(expected_version=plan.version),
            "shared-mask-stale-validation",
        )


@pytest.mark.parametrize(
    "case", ["qc_pending", "mixed_version", "file_change", "manifest_change", "raw_input_change"]
)
def test_receiver_blocks_unready_or_changed_inputs(service, source_root, work_root, case):
    project, run, manifest, _, metrics, root = _source(
        service,
        source_root,
        work_root,
        approve=case != "qc_pending",
        mixed_version=case == "mixed_version",
    )
    frozen = service.prepare_work_analysis_handoff(run.run_id, project.project_id)
    if case == "file_change":
        target = root / metrics[0].relative_path
        target.write_bytes(b"x" * target.stat().st_size)  # Same size, different SHA.
    elif case == "manifest_change":
        dataset = service.repository.get_dataset(manifest.dataset_id)
        service.inspect_dataset(
            dataset.dataset_id,
            ManifestScanRequest(expected_dataset_version=dataset.version),
            "handoff-manifest-new",
        )
    elif case == "raw_input_change":
        dataset = service.repository.get_dataset(manifest.dataset_id)
        target = next(Path(dataset.source_path).rglob("*bold.nii.gz"))
        target.write_bytes(b"x" * target.stat().st_size)
    current = service.prepare_work_analysis_handoff(run.run_id, project.project_id)
    assert current.status == "blocked"
    expected = {
        "qc_pending": "source_run_not_succeeded",
        "mixed_version": "handoff_metric_versions_mixed",
        "file_change": "artifact_integrity_changed",
        "manifest_change": "source_manifest_superseded",
        "raw_input_change": "source_manifest_content_changed",
    }[case]
    assert expected in current.blocking_items
    with pytest.raises(ConflictError) as stale:
        require_same_handoff(frozen, current)
    assert stale.value.code == "work_handoff_stale"


@pytest.mark.parametrize("stage", ["validation", "approval", "start"])
def test_frozen_work_plan_cannot_confirm_after_file_change(service, source_root, work_root, stage):
    project, run, _, subjects, metrics, root = _source(service, source_root, work_root)
    plan, proof = _work_plan(service, project, run, subjects)
    assert plan.plan["work_context"]["analysis_handoff"]["evidence_hash"] == proof.evidence_hash
    if stage in {"approval", "start"}:
        plan = service.validate_statistical_design(
            plan.plan_revision_id,
            StatisticalDesignValidationRequest(expected_version=plan.version),
            "handoff-plan-valid",
        ).plan_revision
    if stage == "start":
        service.approve_plan(
            plan.plan_revision_id,
            ApprovalCreate(
                expected_version=plan.version,
                plan_hash=plan.plan_hash,
                actor="test",
                decision="approved",
                reason="Synthetic fixture",
            ),
            "valid-approval",
        )
    target = root / metrics[0].relative_path
    target.write_bytes(b"x" * target.stat().st_size)
    with pytest.raises(ConflictError) as changed:
        if stage == "validation":
            service.validate_statistical_design(
                plan.plan_revision_id,
                StatisticalDesignValidationRequest(expected_version=plan.version),
                "stale-validate",
            )
        elif stage == "approval":
            service.approve_plan(
                plan.plan_revision_id,
                ApprovalCreate(
                    expected_version=plan.version,
                    plan_hash=plan.plan_hash,
                    actor="test",
                    decision="approved",
                    reason="Synthetic fixture",
                ),
                "stale-approval",
            )
        else:
            service.create_statistics_run(
                StatisticsRunCreate(
                    project_id=project.project_id,
                    statistical_design_revision_id=plan.plan_revision_id,
                    expected_plan_hash=plan.plan_hash,
                    execution_backend="mock",
                ),
                "stale-start",
            )
    assert changed.value.code == ("plan_stale" if stage == "start" else "work_handoff_stale")
    assert len(service.repository.list_runs(project_id=project.project_id)) == 1


def test_two_stage_dialogue_persists_complete_proof_without_leaking_lineage(
    service,
    source_root,
    work_root,
    monkeypatch,
):
    project, run, _, _, _, _ = _source(service, source_root, work_root)
    conversation = service.create_conversation(
        ConversationCreate(mode="work", project_id=project.project_id), "two-stage-conversation"
    )
    packet = ContextPacket(
        question="Pipeline",
        metadata={"token_budget": 12000},
        work_context={"run": {"run_id": run.run_id, "state": "succeeded", "qc_status": "approved"}},
    )
    response = asyncio.run(
        service.work_dialogue.respond(
            conversation,
            ConversationTurnCreate(content="先预处理再比较两组", target_run_id=run.run_id),
            packet,
            workspace_path=None,
            project_id=project.project_id,
            preferred_profile_id=None,
            idempotency_key="two-stage-turn",
            has_profiles=False,
        )
    )
    assert response.payload["agent_handoff"]["status"] == "ready"
    assert response.payload["analysis_dependency"]["status"] == "analysis_ready"
    assert response.payload["agent_handoff"]["data_references"]["metrics"]
    service.repository.append_conversation_exchange(
        conversation.conversation_id,
        user_content="先预处理再比较两组",
        assistant_content=response.assistant_content,
        assistant_payload=response.payload,
        tool=None,
        workspace_path=None,
        preferred_profile_id=None,
        project_id=project.project_id,
        active_run_id=run.run_id,
    )
    restored = service.get_conversation(conversation.conversation_id)
    assert restored.messages[-1].payload["agent_handoff"] == response.payload["agent_handoff"]
    interpretation = AsyncMock(
        return_value=(
            json.dumps({"action": "conversation", "answer": "Discuss analysis"}),
            {},
            None,
        )
    )
    monkeypatch.setattr(service.work_dialogue, "_interpret", interpretation)
    analysis = asyncio.run(
        service.work_dialogue.respond(
            restored,
            ConversationTurnCreate(content="继续"),
            packet,
            workspace_path=None,
            project_id=project.project_id,
            preferred_profile_id=None,
            idempotency_key="two-stage-analysis",
            has_profiles=True,
        )
    )
    assert analysis.payload["agent_role"] == "analysis"
    shared = interpretation.call_args.args[0]["work_context"]["analysis_dependency"]
    assert "metrics" not in shared["data_references"]
    assert shared["data_references"]["metric_count"] > 0
