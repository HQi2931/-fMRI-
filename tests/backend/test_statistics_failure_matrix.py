"""Statistical failure isolation, evidence drift, and resumable Worker attempts.

All maps, files, and executions are synthetic; no MATLAB process is started.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from neuroagent.application.contracts import (
    ApprovalCreate,
    ApprovalDecision,
    ExternalAnalysisImportIncludeRequest,
    ExternalAnalysisImportInspectRequest,
    ExternalDemographicsImportRequest,
    PlanRevisionCreate,
    PlanValidationRequest,
    RunAction,
    StatisticalDesignCreate,
    WorkflowState,
)
from neuroagent.application.errors import ApplicationError
from neuroagent.application.ports import ExecutionResult
from neuroagent.application.services import NeuroAgentService
from neuroagent.application.statistics_draft import StatisticsDraft
from neuroagent.bootstrap import build_worker
from neuroagent.domain.fmri.statistics import (
    MissingValuePolicy,
    StatisticalDesignRevision,
    StatisticalTest,
    Tail,
)
from neuroagent.infrastructure.persistence.models import (
    ArtifactRow,
    PlanRevisionRow,
    ProjectRow,
    WorkflowRunRow,
)
from neuroagent.infrastructure.persistence.statistical_completion import (
    register_real_statistical_result,
)

from .conftest import make_project
from .test_external_analysis_imports import _valid_import_tree, _write_map
from .test_statistical_completion import _artifacts, _design
from .test_work_analysis_handoffs import _source


def _completion(tmp_path: Path) -> tuple[MagicMock, dict[str, Any]]:
    session = MagicMock()
    project = ProjectRow(
        project_id="project-1", name="Synthetic", source_roots_json="[]", work_root=str(tmp_path)
    )
    session.get.side_effect = lambda model, key: project if model is ProjectRow else None
    design = _design()
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
    return session, {
        "run": WorkflowRunRow(
            run_id="run-1", project_id="project-1", plan_revision_id="plan-1", state="running"
        ),
        "plan": plan,
        "payload": {
            "plan_hash": plan.plan_hash,
            "input_manifest_hash": plan.manifest_hash,
            "statistical_design": design,
            "correction": None,
        },
        "artifact_rows": _artifacts(tmp_path),
        "actor": "synthetic-worker",
        "created_at": datetime.now(UTC),
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("plan_hash", "plan hash"),
        ("manifest_hash", "manifest hash"),
        ("baseline", "design does not match"),
        ("frozen_missing", "typed design"),
        ("frozen_not_object", "typed design"),
        ("correction_not_object", "must be an object"),
        ("unknown_correction", "unsupported correction"),
        ("unexpected_artifact", "unexpected statistical artifact"),
        ("project_missing", "project is unavailable"),
    ],
)
def test_statistical_completion_binding_drift_stages_nothing(
    tmp_path: Path, mutation: str, message: str
) -> None:
    session, args = _completion(tmp_path)
    payload = args["payload"]
    if mutation == "plan_hash":
        payload["plan_hash"] = "f" * 64
    elif mutation == "manifest_hash":
        payload["input_manifest_hash"] = "f" * 64
    elif mutation == "baseline":
        payload["statistical_design"] = {**payload["statistical_design"], "one_sample_baseline": 1}
    elif mutation == "frozen_missing":
        args["plan"].plan_json = "{}"
    elif mutation == "frozen_not_object":
        args["plan"].plan_json = "[]"
    elif mutation == "correction_not_object":
        payload["correction"] = "fdr"
    elif mutation == "unknown_correction":
        payload["correction"] = {"method": "unvalidated"}
    elif mutation == "unexpected_artifact":
        args["artifact_rows"][0].artifact_type = "statistics.unvalidated_output"
    elif mutation == "project_missing":
        session.get.side_effect = None
        session.get.return_value = None
    with pytest.raises(ValueError, match=message):
        register_real_statistical_result(session, **args)
    session.add.assert_not_called()


@pytest.mark.parametrize(
    "relative_path",
    [
        "/output/table.tsv",
        "../table.tsv",
        "output/../table.tsv",
        "output\\table.tsv",
        "output//table.tsv",
        "C:/table.tsv",
    ],
)
def test_statistical_completion_rejects_noncanonical_result_paths(
    tmp_path: Path, relative_path: str
) -> None:
    session, args = _completion(tmp_path)
    table = next(x for x in args["artifact_rows"] if x.artifact_type == "statistics.cluster_table")
    table.relative_path = relative_path
    with pytest.raises(ValueError, match=r"canonical|inside the run directory"):
        register_real_statistical_result(session, **args)
    session.add.assert_not_called()


@pytest.mark.parametrize("damage", ["missing", "size", "checksum", "same_size_content"])
def test_statistical_completion_rejects_unavailable_or_changed_table(
    tmp_path: Path, damage: str
) -> None:
    session, args = _completion(tmp_path)
    table = next(x for x in args["artifact_rows"] if x.artifact_type == "statistics.cluster_table")
    path = tmp_path / "run-1" / table.relative_path
    if damage == "missing":
        path.unlink()
    elif damage == "size":
        table.size_bytes += 1
    elif damage == "checksum":
        table.checksum = "f" * 64
    else:
        content = path.read_bytes()
        path.write_bytes(b"X" + content[1:])
        assert path.stat().st_size == table.size_bytes
    with pytest.raises(ValueError, match=r"unavailable|integrity"):
        register_real_statistical_result(session, **args)
    session.add.assert_not_called()


@pytest.mark.parametrize("table", [b"1\t0\t0\t0\t2\t4\n", b"1\t0\t0\t0\t\t4\n"])
def test_corrected_voxels_cannot_disagree_with_cluster_extent(tmp_path: Path, table: bytes) -> None:
    session, args = _completion(tmp_path)
    args["payload"]["correction"] = {
        "method": "fdr",
        "q_threshold": 0.05,
        "mask_artifact_id": "mask-1",
        "statistic_type": "T",
        "df1": 2,
        "df2": None,
    }
    values = np.zeros((2, 2, 2), dtype=np.float32)
    values[0, 0, 0] = 4
    rows = _artifacts(tmp_path, corrected_values=values)
    args["artifact_rows"] = rows
    row = next(x for x in rows if x.artifact_type == "statistics.cluster_table")
    path = tmp_path / "run-1" / row.relative_path
    content = b"cluster\tpeak_x\tpeak_y\tpeak_z\tvoxels\tstatistic\n" + table
    path.write_bytes(content)
    row.checksum = hashlib.sha256(content).hexdigest()
    row.size_bytes = len(content)
    with pytest.raises(ValueError, match="voxel count"):
        register_real_statistical_result(session, **args)
    session.add.assert_not_called()


@pytest.mark.parametrize(
    "role", ["correction_evidence", "corrected_z_map", "corrected_statistical_map"]
)
@pytest.mark.parametrize("damage", ["missing", "content"])
def test_grf_completion_checks_all_registered_correction_files_before_success(
    tmp_path: Path, role: str, damage: str
) -> None:
    session, args = _completion(tmp_path)
    args["payload"]["correction"] = {
        "method": "grf",
        "voxel_p_threshold": 0.001,
        "cluster_p_threshold": 0.05,
        "two_tailed": True,
        "mask_artifact_id": "mask-1",
        "statistic_type": "T",
        "df1": 2,
        "df2": None,
        "smoothness_mode": "provided_dlh",
        "smoothness_dlh": 0.05,
    }
    rows = _artifacts(tmp_path, corrected_values=np.zeros((2, 2, 2), dtype=np.float32))
    z_relative = "output/corrected_z_map.nii"
    z_path = tmp_path / "run-1" / z_relative
    # A complete artifact contract is necessary before the corruption guard is reachable.
    corrected = next(x for x in rows if x.artifact_type == "statistics.corrected_statistical_map")
    z_path.write_bytes((tmp_path / "run-1" / corrected.relative_path).read_bytes())
    rows.append(
        ArtifactRow(
            artifact_id="artifact-corrected-z",
            project_id="project-1",
            run_id="run-1",
            artifact_type="statistics.corrected_z_map",
            relative_path=z_relative,
            checksum=hashlib.sha256(z_path.read_bytes()).hexdigest(),
            size_bytes=z_path.stat().st_size,
            provenance_json=json.dumps({"executor": "synthetic"}),
        )
    )
    args["artifact_rows"] = rows
    target = next(x for x in rows if x.artifact_type == f"statistics.{role}")
    target_path = tmp_path / "run-1" / target.relative_path
    if damage == "missing":
        target_path.unlink()
    else:
        original = target_path.read_bytes()
        target_path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        assert target_path.stat().st_size == target.size_bytes
    with pytest.raises(ValueError, match=r"unavailable|integrity"):
        register_real_statistical_result(session, **args)
    session.add.assert_not_called()


class _SyntheticExecutor:
    def __init__(self, execute: Callable[[dict[str, Any], Callable[[], bool]], ExecutionResult]):
        self.callback = execute
        self.attempts: list[int] = []

    def execute(
        self, payload: dict[str, Any], *, is_cancelled: Callable[[], bool]
    ) -> ExecutionResult:
        self.attempts.append(len(self.attempts) + 1)
        return self.callback(payload, is_cancelled)


def _statistics_run(service, source_root, work_root, *, attempts=2):
    project = make_project(service, source_root, work_root)
    plan = service.create_plan(
        PlanRevisionCreate(
            project_id=project.project_id,
            expected_project_version=project.version,
            plan={"kind": "statistical_design", "design": _design()},
            manifest_hash="a" * 64,
            environment_hash="b" * 64,
        ),
        "statistical-fixture-plan",
    )
    plan = service.validate_plan(
        plan.plan_revision_id,
        PlanValidationRequest(expected_version=plan.version, issues=[]),
        "validate-fixture",
    )
    service.approve_plan(
        plan.plan_revision_id,
        ApprovalCreate(
            expected_version=plan.version,
            plan_hash=plan.plan_hash,
            actor="fixture",
            decision=ApprovalDecision.APPROVED,
            reason="Synthetic failure protocol fixture",
        ),
        "approve-fixture",
    )
    return service.repository.create_run(
        project_id=project.project_id,
        plan_revision_id=plan.plan_revision_id,
        expected_plan_hash=plan.plan_hash,
        max_attempts=attempts,
        payload={
            "run_kind": "statistics_matlab",
            "statistical_design": _design(),
            "correction": None,
            "plan_hash": plan.plan_hash,
            "input_manifest_hash": plan.manifest_hash,
        },
    )


@pytest.mark.parametrize(
    "status", ["cancelled", "timed_out", "failed_retryable", "failed_terminal"]
)
def test_failed_statistics_worker_preserves_only_diagnostic_logs(
    service: NeuroAgentService, source_root: Path, work_root: Path, status: str
) -> None:
    run = _statistics_run(service, source_root, work_root)
    partial = {
        "artifact_type": "statistics.corrected_statistical_map",
        "relative_path": "output/partial.nii",
        "checksum": "a" * 64,
        "size_bytes": 1,
        "provenance": {"executor": "synthetic"},
    }
    log = {
        **partial,
        "artifact_type": "execution.matlab_log",
        "relative_path": "logs/attempt-001.log",
    }
    executor = _SyntheticExecutor(
        lambda _payload, _cancel: ExecutionResult(
            status=status, error="synthetic failure", artifacts=(partial, log)
        )
    )
    assert build_worker(service, worker_id="failure-matrix", executor=executor).run_once()
    expected = {
        "cancelled": WorkflowState.CANCELLED,
        "timed_out": WorkflowState.TIMED_OUT,
        "failed_retryable": WorkflowState.FAILED_RETRYABLE,
        "failed_terminal": WorkflowState.FAILED_TERMINAL,
    }[status]
    assert service.get_run(run.run_id).state is expected
    assert [x.artifact_type for x in service.list_artifacts(run.run_id)] == ["execution.matlab_log"]
    assert service.list_statistical_results(project_id=run.project_id, run_id=run.run_id) == []
    assert not any(
        e.event_type == "StatisticalResultRegistered" for e in service.list_run_events(run.run_id)
    )


@pytest.mark.parametrize("status", ["timed_out", "failed_retryable"])
def test_statistics_worker_retry_budget_exhaustion_has_no_scientific_success(
    service: NeuroAgentService, source_root: Path, work_root: Path, status: str
) -> None:
    run = _statistics_run(service, source_root, work_root)
    executor = _SyntheticExecutor(
        lambda _payload, _cancel: ExecutionResult(status=status, error="retry")
    )
    worker = build_worker(service, worker_id="retry-matrix", executor=executor)
    assert worker.run_once()
    first = service.get_run(run.run_id)
    expected = WorkflowState.TIMED_OUT if status == "timed_out" else WorkflowState.FAILED_RETRYABLE
    assert first.state is expected
    service.retry_run(
        run.run_id, RunAction(expected_version=first.version, reason="Synthetic retry"), "retry"
    )
    assert worker.run_once()
    assert executor.attempts == [1, 2]
    assert service.get_run(run.run_id).state is WorkflowState.FAILED_TERMINAL
    assert worker.run_once() is False
    assert service.list_statistical_results(project_id=run.project_id, run_id=run.run_id) == []
    assert service.list_artifacts(run.run_id) == []


@pytest.mark.parametrize("returned_status", ["succeeded", "failed_retryable", "timed_out"])
def test_statistics_cancellation_wins_completion_race_without_result_registration(
    service: NeuroAgentService, source_root: Path, work_root: Path, returned_status: str
) -> None:
    run = _statistics_run(service, source_root, work_root)

    def cancel_during_execution(_payload, is_cancelled):
        active = service.get_run(run.run_id)
        service.cancel_run(
            run.run_id,
            RunAction(expected_version=active.version, reason="Cancel fixture"),
            "cancel",
        )
        assert is_cancelled()
        return ExecutionResult(status=returned_status)

    executor = _SyntheticExecutor(cancel_during_execution)
    assert build_worker(service, worker_id="cancel-race", executor=executor).run_once()
    assert service.get_run(run.run_id).state is WorkflowState.CANCELLED
    assert service.list_artifacts(run.run_id) == []
    assert service.list_statistical_results(project_id=run.project_id, run_id=run.run_id) == []


def test_success_claim_with_partial_statistics_is_rolled_back_and_terminalized(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    run = _statistics_run(service, source_root, work_root)
    partial = {
        "artifact_type": "statistics.uncorrected_statistical_map",
        "relative_path": "output/partial.nii",
        "checksum": "a" * 64,
        "size_bytes": 1,
    }
    executor = _SyntheticExecutor(
        lambda _payload, _cancel: ExecutionResult(status="succeeded", artifacts=(partial,))
    )
    assert build_worker(service, worker_id="partial-success", executor=executor).run_once()
    completed = service.get_run(run.run_id)
    assert completed.state is WorkflowState.FAILED_TERMINAL
    assert completed.error == "artifact finalization failed: ValidationError"
    assert service.list_artifacts(run.run_id) == []
    assert service.list_statistical_results(project_id=run.project_id, run_id=run.run_id) == []
    transitions = [
        e.payload["to_state"]
        for e in service.list_run_events(run.run_id)
        if e.event_type == "WorkflowTransitioned"
    ]
    assert "succeeded" not in transitions


def _external_fixture(service, source_root, work_root):
    project = make_project(service, source_root, work_root)
    source = source_root / "synthetic-external"
    subjects = _valid_import_tree(source)
    _write_map(source / "Mask.nii")
    inspected = service.inspect_external_analysis_import(
        ExternalAnalysisImportInspectRequest(
            project_id=project.project_id, source_path=str(source)
        ),
        "inspect-fixture",
    )
    imported = service.include_external_analysis_import(
        inspected.import_id,
        ExternalAnalysisImportIncludeRequest(
            expected_version=inspected.version,
            included_subject_ids=subjects,
            group_order=("ASD", "HC"),
            coordinate_space="unknown",
            acknowledge_unknown_processing=True,
            actor="fixture",
            reason="Synthetic failure matrix",
            mask_relative_path="Mask.nii",
        ),
        "include-fixture",
    )
    draft = StatisticsDraft(
        test=StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
        grouping_column="group",
        group_order=["ASD", "HC"],
        metric_artifact_type="external.alff.z_score",
        tail=Tail.TWO_SIDED,
        missing_value_policy=MissingValuePolicy.ERROR,
        correction_method="fdr",
        correction_parameters={"q_threshold": 0.05},
    )
    return project, source, imported, draft


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        ("project", "cross_project_external_import"),
        ("tail", "external_method_choices_missing"),
        ("missing_policy", "external_method_choices_missing"),
        ("paired", "external_import_layout_mismatch"),
        ("group_column", "external_group_column_invalid"),
        ("metric", "external_metric_type_mismatch"),
        ("covariate", "external_demographics_required"),
        ("source_deleted", "external_import_source_changed"),
        ("source_replaced", "external_import_source_changed"),
    ],
)
def test_external_statistical_proposal_rejects_invalid_or_stale_source(
    service: NeuroAgentService, source_root: Path, work_root: Path, change: str, expected_code: str
) -> None:
    project, source, imported, draft = _external_fixture(service, source_root, work_root)
    project_id = project.project_id
    if change == "project":
        project_id = "different-project"
    elif change == "tail":
        draft.tail = None
    elif change == "missing_policy":
        draft.missing_value_policy = None
    elif change == "paired":
        draft.test = StatisticalTest.PAIRED_T
    elif change == "group_column":
        draft.grouping_column = "undeclared"
    elif change == "metric":
        draft.metric_artifact_type = "external.reho.z_score"
    elif change == "covariate":
        draft.covariates = [{"name": "age", "centering": "grand_mean"}]
    elif change == "source_deleted":
        (source / "ASD" / "sub-01-zALFFMap.nii").unlink()
    elif change == "source_replaced":
        _write_map(source / "ASD" / "sub-01-zALFFMap.nii", offset=25)
    proposal, issues = service.prepare_work_statistical_design_proposal(
        external_import_id=imported.import_id, project_id=project_id, draft=draft
    )
    assert proposal is None
    assert [issue.code for issue in issues] == [expected_code]
    # Proposal rejection must not turn an explicitly included import into a run.
    assert service.list_runs(project_id=project.project_id) == []
    assert (
        service.get_external_analysis_import(imported.import_id).inclusion_hash
        == imported.inclusion_hash
    )


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        ("mask_run", "statistics_mask_run_mismatch"),
        ("mask_subject", "statistics_mask_lineage_invalid"),
        ("mask_manifest", "statistics_mask_lineage_invalid"),
        ("mask_unverified", "statistics_mask_lineage_invalid"),
        ("metric_run", "statistics_artifact_run_mismatch"),
        ("missing_lineage", "artifact_lineage_missing"),
        ("invalid_lineage", "artifact_lineage_invalid"),
        ("identity", "artifact_lineage_identity_mismatch"),
        ("metric_manifest", "statistics_artifact_manifest_mismatch"),
        ("subject", "statistics_artifact_subject_mismatch"),
        ("condition", "statistics_artifact_condition_mismatch"),
        ("mask_reference", "statistics_metric_mask_mismatch"),
        ("grid", "statistics_metric_grid_mismatch"),
        ("space", "statistics_metric_series_mismatch"),
        ("scaling", "statistics_metric_series_mismatch"),
        ("band", "statistics_metric_series_mismatch"),
        ("smoothing", "statistics_metric_series_mismatch"),
        ("session", "statistics_metric_series_mismatch"),
        ("scrubbing", "statistics_metric_series_mismatch"),
    ],
)
def test_internal_statistics_rejects_mixed_or_misaligned_registered_lineage(
    service: NeuroAgentService,
    source_root: Path,
    work_root: Path,
    monkeypatch,
    change: str,
    expected_code: str,
) -> None:
    _project, run, manifest, subjects, metrics, _root = _source(service, source_root, work_root)
    by_subject = {item.provenance["lineage"]["subject_id"]: item for item in metrics}
    mask_id = metrics[0].provenance["lineage"]["mask_artifact_id"]
    design = StatisticalDesignRevision.model_validate(
        {
            **_design(),
            "subject_order": subjects,
            "images": [
                {
                    "subject_id": subject,
                    "artifact_id": by_subject[subject].artifact_id,
                    "group": None,
                    "condition": None,
                }
                for subject in subjects
            ],
            "mask_artifact_id": mask_id,
        }
    )
    mask = service.get_artifact(mask_id)
    # Change the second image to exercise compatibility with the first frozen image.
    target = (
        mask
        if change in {"mask_run", "mask_subject", "mask_manifest", "mask_unverified"}
        else by_subject[subjects[1]]
    )
    fields = target.model_dump(mode="python")
    lineage = dict(fields["provenance"]["lineage"])
    if change in {"mask_run", "metric_run"}:
        fields["run_id"] = "unrelated-run"
    elif change == "missing_lineage":
        fields["provenance"] = {}
    elif change == "invalid_lineage":
        lineage["metadata_evidence_hash"] = "invalid"
    elif change == "identity":
        lineage["artifact_id"] = "unrelated-artifact"
    elif change in {"mask_manifest", "metric_manifest"}:
        lineage["subject_manifest_hash"] = "f" * 64
    elif change == "mask_subject":
        lineage["subject_id"] = subjects[0]
    elif change == "mask_unverified":
        lineage["metadata_verified"] = False
        lineage["metadata_evidence_hash"] = None
    elif change == "subject":
        lineage["subject_id"] = subjects[0]
    elif change == "condition":
        lineage["condition"] = "unregistered-condition"
    elif change == "mask_reference":
        lineage["mask_artifact_id"] = "unrelated-mask"
    elif change == "grid":
        lineage["grid_signature"] = lineage["mask_grid_signature"] = "different-grid"
    elif change == "space":
        lineage["space"] = "native"
    elif change == "scaling":
        lineage["metric_scaling"] = "raw"
    elif change == "band":
        lineage["frequency_band"] = {"low_hz": 0.02, "high_hz": 0.09}
    elif change == "smoothing":
        lineage["spatially_smoothed"] = True
        lineage["smoothing_fwhm_mm"] = [6, 6, 6]
    elif change == "session":
        lineage["session_id"] = "different-session"
    elif change == "scrubbing":
        lineage["scrubbed"] = True
    if change != "missing_lineage":
        fields["provenance"] = {**fields["provenance"], "lineage": lineage}
    altered = type(target).model_validate(fields)
    original_get = service.repository.get_artifact
    monkeypatch.setattr(
        service.repository,
        "get_artifact",
        lambda artifact_id: (
            altered if artifact_id == target.artifact_id else original_get(artifact_id)
        ),
    )
    with pytest.raises(ApplicationError) as error:
        service._validate_statistical_artifact_lineage(
            design, manifest_hash=manifest.content_hash, source_run_id=run.run_id
        )
    assert error.value.code == expected_code
    # Simulated observation drift never mutates the original reviewed synthetic QC.
    assert original_get(target.artifact_id) == target


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        ("unknown_column", "covariate_column_missing"),
        ("nonnumeric", "covariate_value_missing"),
        ("rank", "design_rejected_by_domain"),
    ],
)
def test_external_covariate_failures_never_emit_an_executable_proposal(
    service: NeuroAgentService, source_root: Path, work_root: Path, change: str, expected_code: str
) -> None:
    project, _source, imported, draft = _external_fixture(service, source_root, work_root)
    values = (
        ("bad", "23", "25", "29")
        if change == "nonnumeric"
        else (("20", "20", "20", "20") if change == "rank" else ("20", "23", "25", "29"))
    )
    table = source_root / "synthetic-demographics.csv"
    table.write_text(
        "ID,Age\n" + "".join(f"sub-{i:02d},{value}\n" for i, value in enumerate(values, 1)),
        encoding="utf-8",
    )
    service.import_external_demographics(
        imported.import_id,
        ExternalDemographicsImportRequest(
            source_path=str(table),
            subject_id_column="ID",
            column_mapping={"age": "Age"},
            expected_import_version=imported.version,
        ),
        "demographics-fixture",
    )
    draft = StatisticsDraft.model_validate(
        {
            **draft.model_dump(mode="python"),
            "covariates": [
                {
                    "name": "missing" if change == "unknown_column" else "age",
                    "centering": "grand_mean",
                }
            ],
        }
    )
    proposal, issues = service.prepare_work_statistical_design_proposal(
        external_import_id=imported.import_id, project_id=project.project_id, draft=draft
    )
    assert proposal is None
    assert expected_code in [issue.code for issue in issues]
    assert service.list_runs(project_id=project.project_id) == []


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        ("manifest", "external_import_manifest_mismatch"),
        ("inclusion", "external_import_not_selectable"),
        ("mask", "external_import_mask_reference_invalid"),
        ("image", "external_import_image_reference_invalid"),
        ("subjects", "external_import_pair_alignment_failed"),
        ("groups", "external_import_group_alignment_failed"),
        ("labels", "external_import_label_alignment_failed"),
    ],
)
def test_formal_external_design_cannot_override_server_bound_inputs(
    service: NeuroAgentService, source_root: Path, work_root: Path, change: str, expected_code: str
) -> None:
    project, _source, imported, draft = _external_fixture(service, source_root, work_root)
    proposal, issues = service.prepare_work_statistical_design_proposal(
        external_import_id=imported.import_id, project_id=project.project_id, draft=draft
    )
    assert proposal is not None and not issues
    design = proposal.design.model_dump(mode="json")
    manifest_hash = proposal.input_manifest_hash
    if change == "manifest":
        manifest_hash = "f" * 64
    elif change == "inclusion":
        design["external_import_hash"] = "f" * 64
    elif change == "mask":
        design["mask_artifact_id"] = "unregistered-mask"
    elif change == "image":
        design["images"][0]["artifact_id"] = "unregistered-image"
    elif change == "subjects":
        design["subject_order"] = design["subject_order"][1:]
        design["images"] = design["images"][1:]
    elif change == "groups":
        design["group_order"] = ["ASD", "unregistered-group"]
        for image in design["images"]:
            if image["group"] == "HC":
                image["group"] = "unregistered-group"
    elif change == "labels":
        for image in design["images"]:
            image["group"] = "ASD" if image["group"] == "HC" else "HC"
    changed = StatisticalDesignRevision.model_validate(design)
    with pytest.raises(ApplicationError) as error:
        service.create_statistical_design(
            StatisticalDesignCreate(
                project_id=project.project_id,
                expected_project_version=proposal.project_version,
                input_manifest_hash=manifest_hash,
                design=changed,
                correction=None,
            ),
            "invalid-design",
        )
    assert error.value.code == expected_code
    assert service.get_project(project.project_id).version == proposal.project_version
    assert service.list_runs(project_id=project.project_id) == []
