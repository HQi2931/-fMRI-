from __future__ import annotations

import pytest

from neuroagent.application.contracts.statistics import (
    StatisticalDesignCreate,
)
from neuroagent.application.statistics_draft import StatisticsDraft
from neuroagent.domain.lifecycle import (
    WorkflowState,
)

from .test_work_analysis_handoffs import _source


@pytest.mark.parametrize("source_state", [WorkflowState.SUCCEEDED, WorkflowState.FAILED_TERMINAL])
def test_completed_producer_environment_is_historical_but_consumer_remains_current(
    service, source_root, work_root, monkeypatch, source_state
):
    project, source_run, _, _, _, _ = _source(service, source_root, work_root)
    proposal, issues = service.statistics.prepare_statistical_design_proposal(
        run_id=source_run.run_id,
        draft=StatisticsDraft(
            test="one_sample_t",
            one_sample_baseline=0,
            metric_artifact_type="metric.synthetic.alff",
            tail="two_sided",
            missing_value_policy="error",
        ),
    )
    assert proposal is not None and not issues
    created = service.statistics.create_statistical_design(
        StatisticalDesignCreate(
            project_id=project.project_id,
            expected_project_version=proposal.project_version,
            input_manifest_hash=proposal.input_manifest_hash,
            design=proposal.design,
        ),
        "producer-environment-statistics",
    )
    consumer = created.plan_revision
    producer = service.plans.get_plan(source_run.plan_revision_id)
    original_plan = service.statistics.plan_repository.get_plan
    original_run = service.statistics.run_repository.get_run
    # The lightweight producer's Skill/input checks are covered separately;
    # isolate the historical environment boundary using the real QC/design.
    historical = producer.model_copy(
        update={"environment_hash": "a" * 64, "plan": {**producer.plan, "kind": "skill_plan"}}
    )
    monkeypatch.setattr(
        service.statistics.plan_repository,
        "get_plan",
        lambda key: historical if key == producer.plan_revision_id else original_plan(key),
    )
    monkeypatch.setattr(
        service.statistics.run_repository,
        "get_run",
        lambda key: (
            original_run(key).model_copy(update={"state": source_state})
            if key == source_run.run_id
            else original_run(key)
        ),
    )
    monkeypatch.setattr(
        service.statistics,
        "_skill_plan_staleness_reasons",
        lambda plan, reasons, verify: reasons,
    )
    reasons = service.statistics._plan_staleness_reasons(
        consumer, current_environment_hash=consumer.environment_hash, verify_source_files=False
    )
    assert any(item["code"] == "environment_lock_changed" for item in reasons) is (
        source_state is not WorkflowState.SUCCEEDED
    )
    # A changed current environment still invalidates the consumer's approval.
    changed = service.statistics._plan_staleness_reasons(
        consumer, current_environment_hash="f" * 64, verify_source_files=False
    )
    assert any(item["code"] == "environment_lock_changed" for item in changed)
