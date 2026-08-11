from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from neuroagent.api import create_app
from neuroagent.application.contracts import RunCreate
from neuroagent.application.services import NeuroAgentService

from .conftest import make_approved_plan, make_project


def _headers() -> dict[str, str]:
    return {"Idempotency-Key": "extension-api-key"}


def test_extension_analysis_endpoints_are_local_and_typed(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root)
    table = source_root / "features.csv"
    table.write_text("subject_id,group,roi_1\nsub-01,a,0.1\nsub-02,b,0.2\n", encoding="utf-8")
    plan = make_approved_plan(service, project.project_id)
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
        ),
        "extension-run-key",
    )

    with TestClient(create_app(service=service)) as client:
        diagnosis = client.post(
            f"/api/v1/runs/{run.run_id}/diagnosis",
            headers=_headers(),
            json={"log_text": "Undefined function spm_vol"},
        )
        assert diagnosis.status_code == 200
        assert diagnosis.json()["diagnosis"]["code"] == "software_environment"

        inspected = client.post(
            "/api/v1/ml/datasets/inspect",
            headers=_headers(),
            json={"project_id": project.project_id, "source_path": str(table)},
        )
        assert inspected.status_code == 200
        assert inspected.json()["inspection"]["subject_candidates"] == ["subject_id"]

        template = client.post(
            "/api/v1/ml/templates",
            headers=_headers(),
            json={
                "design": {
                    "target_column": "group",
                    "group_column": "subject_id",
                    "feature_columns": ["roi_1"],
                    "models": ["logistic_regression"],
                    "seed": 42,
                    "validation_strategy": "subject_grouped_stratified_cross_validation",
                    "metrics": ["roc_auc"],
                    "warnings": [],
                    "requires_approval": True,
                },
                "source_filename": "features.csv",
            },
        )
        assert template.status_code == 200
        assert "StratifiedGroupKFold" in template.json()["template"]["content"]

        roi = client.post(
            "/api/v1/roi/extractions/validate",
            headers=_headers(),
            json={
                "design": {
                    "input_artifact_id": "functional",
                    "atlas_artifact_id": "atlas",
                    "tr_seconds": 2,
                    "band_low_hz": 0.01,
                    "band_high_hz": 0.08,
                    "multiple_labels": True,
                    "detrend": True,
                    "scrubbing_timing": "disabled",
                },
                "records": [
                    {
                        "subject_id": "sub-01",
                        "metric": "roi_signal",
                        "atlas_id": "atlas",
                        "roi_index": 1,
                        "roi_label": "Frontal",
                        "value": 0.1,
                    }
                ],
            },
        )
        assert roi.status_code == 200
        assert roi.json()["valid"] is True

        localized = client.post(
            "/api/v1/cluster-localizations",
            headers=_headers(),
            json={
                "clusters": [{"cluster_id": "1", "peak_x": 0, "peak_y": 0, "peak_z": 0}],
                "atlas_points": [{"x": 0, "y": 0, "z": 0, "label": "Example"}],
            },
        )
        assert localized.status_code == 200
        assert localized.json()["results"][0]["atlas_label"] == "Example"

        answer = client.post(
            "/api/v1/agent/rsfmri/questions",
            headers=_headers(),
            json={"question": "What is ALFF in rs-fMRI?", "allow_remote_search": False},
        )
        assert answer.status_code == 200
        assert answer.json()["remote_search_used"] is False
