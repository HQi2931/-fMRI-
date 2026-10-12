from __future__ import annotations

from pathlib import Path

from neuroagent.application.services import ApplicationServices
from neuroagent.bootstrap import build_worker
from neuroagent.domain.lifecycle import (
    WorkflowState,
)

from .conftest import make_approved_plan, make_project


def test_statistics_mock_success_skips_metric_qc_without_claiming_real_outputs(
    service: ApplicationServices,
    source_root: Path,
    work_root: Path,
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run = service.repository.create_run(
        project_id=project.project_id,
        plan_revision_id=plan.plan_revision_id,
        expected_plan_hash=plan.plan_hash,
        max_attempts=1,
        payload={
            "outcome": "succeed",
            "delay_ms": 0,
            "run_kind": "statistics_mock",
        },
    )

    assert build_worker(service, worker_id="statistics-mock-worker").run_once() is True

    completed = service.runs.get_run(run.run_id)
    assert completed.state is WorkflowState.SUCCEEDED
    artifacts = service.runs.list_artifacts(run.run_id)
    assert len(artifacts) == 1
    assert artifacts[0].artifact_type == "mock.result"
    assert artifacts[0].provenance["executor"] == "mock"
    assert "lineage" not in artifacts[0].provenance
    transitions = [
        event
        for event in service.runs.list_run_events(run.run_id)
        if event.event_type == "WorkflowTransitioned"
    ]
    assert transitions[-1].payload["to_state"] == "succeeded"
    assert transitions[-1].payload["synthetic"] is True
