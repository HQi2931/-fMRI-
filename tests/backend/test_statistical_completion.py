from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from neuroagent.domain.fmri.statistics import StatisticalTest
from neuroagent.infrastructure.persistence.models import (
    ArtifactRow,
    PlanRevisionRow,
    WorkflowRunRow,
)
from neuroagent.infrastructure.persistence.statistical_completion import (
    register_real_statistical_result,
)


def _design() -> dict[str, object]:
    subjects = ("sub-01", "sub-02", "sub-03")
    return {
        "revision_id": "design-1",
        "test": StatisticalTest.ONE_SAMPLE_T.value,
        "subject_order": subjects,
        "images": [
            {
                "subject_id": subject,
                "artifact_id": f"image-{index}",
                "group": None,
                "condition": None,
            }
            for index, subject in enumerate(subjects, start=1)
        ],
        "group_order": [],
        "condition_order": [],
        "covariates": [],
        "contrast": [1.0],
        "one_sample_baseline": 0.0,
        "mask_artifact_id": "mask-1",
        "tail": "two_sided",
        "missing_value_policy": "error",
        "qc_review_revision_id": "qc-1",
        "qc_review_hash": "a" * 64,
    }


def _artifacts() -> list[ArtifactRow]:
    names = (
        "design_matrix",
        "contrast",
        "uncorrected_statistical_map",
        "effect_map",
        "cluster_table",
        "execution_log",
        "software_version_evidence",
    )
    return [
        ArtifactRow(
            artifact_id=f"artifact-{name}",
            project_id="project-1",
            run_id="run-1",
            artifact_type=f"statistics.{name}",
            relative_path=f"output/{name}.dat",
            checksum="b" * 64,
            size_bytes=10,
            provenance_json=json.dumps({"executor": "controlled_matlab"}),
        )
        for name in names
    ]


def test_real_completion_stages_result_and_event_atomically() -> None:
    session = MagicMock()
    design = _design()
    run = WorkflowRunRow(
        run_id="run-1", project_id="project-1", plan_revision_id="plan-1", state="running"
    )
    plan = PlanRevisionRow(
        plan_revision_id="plan-1",
        project_id="project-1",
        revision=1,
        state="approved",
        plan_hash="c" * 64,
        manifest_hash="d" * 64,
        environment_hash="e" * 64,
        plan_json=json.dumps({"kind": "statistical_design", "design": design}),
    )
    result, event = register_real_statistical_result(
        session,
        run=run,
        plan=plan,
        payload={
            "plan_hash": plan.plan_hash,
            "input_manifest_hash": plan.manifest_hash,
            "statistical_design": design,
            "correction": None,
        },
        artifact_rows=_artifacts(),
        actor="worker-1",
        created_at=datetime.now(UTC),
    )

    assert result.mode == "real"
    assert result.non_scientific is False
    assert json.loads(result.manifest_json)["mode"] == "real"
    assert event.event_type == "StatisticalResultRegistered"
    assert session.add.call_count == 2


def test_real_completion_fails_closed_when_a_required_role_is_missing() -> None:
    session = MagicMock()
    design = _design()
    run = WorkflowRunRow(
        run_id="run-1", project_id="project-1", plan_revision_id="plan-1", state="running"
    )
    plan = PlanRevisionRow(
        plan_revision_id="plan-1",
        project_id="project-1",
        revision=1,
        state="approved",
        plan_hash="c" * 64,
        manifest_hash="d" * 64,
        environment_hash="e" * 64,
        plan_json=json.dumps({"kind": "statistical_design", "design": design}),
    )
    with pytest.raises(ValueError, match="incomplete"):
        register_real_statistical_result(
            session,
            run=run,
            plan=plan,
            payload={
                "plan_hash": plan.plan_hash,
                "input_manifest_hash": plan.manifest_hash,
                "statistical_design": design,
                "correction": None,
            },
            artifact_rows=_artifacts()[:-1],
            actor="worker-1",
            created_at=datetime.now(UTC),
        )
    session.add.assert_not_called()
