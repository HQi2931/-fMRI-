from __future__ import annotations

from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
from sqlalchemy import inspect, text

from neuroagent.analysis.external_metric_import import inspect_external_metric_directory
from neuroagent.application.contracts.external_imports import (
    ExternalAnalysisImportIncludeRequest,
    ExternalAnalysisImportInspectRequest,
)
from neuroagent.application.contracts.projects import ProjectCreate
from neuroagent.application.errors import InputValidationError
from neuroagent.application.services import ApplicationServices
from neuroagent.application.statistics_draft import (
    CovariateChoice,
    DesignSubject,
    QcBinding,
    StatisticsChoices,
    StatisticsDraft,
    assemble_statistical_design,
)
from neuroagent.domain.fmri.statistics import (
    MissingValuePolicy,
    StatisticalQcSource,
    StatisticalTest,
    Tail,
)


def _write_map(path: Path, offset: float = 0.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.arange(4 * 5 * 6, dtype=np.float32).reshape((4, 5, 6)) + offset
    nib.save(nib.Nifti1Image(data, np.eye(4)), str(path))


def _project(service: ApplicationServices, source_root: Path, work_root: Path) -> str:
    project = service.projects.create_project(
        ProjectCreate(
            name="Single-group external import",
            source_roots=[str(source_root)],
            work_root=str(work_root / "single-group-project"),
        ),
        "single-group-project-key",
    )
    return project.project_id


def _table_counts(service: ApplicationServices) -> dict[str, int]:
    engine = service.database.engine
    with engine.connect() as connection:
        return {
            table: int(connection.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one())
            for table in inspect(engine).get_table_names()
        }


def _file_hashes(root: Path) -> dict[str, str]:
    import hashlib

    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_single_explicit_group_directory_preserves_subject_identity(
    service: ApplicationServices, source_root: Path, work_root: Path
) -> None:
    project_id = _project(service, source_root, work_root)
    source = source_root / "single-group"
    expected_subjects = ("site-A-sub-01", "site-A-sub-02", "site-B-sub-01")
    for index, subject_id in enumerate(expected_subjects):
        _write_map(source / "cohort" / f"{subject_id}-zALFFMap.nii", offset=float(index))

    inspected = service.external_imports.inspect_external_analysis_import(
        ExternalAnalysisImportInspectRequest(project_id=project_id, source_path=str(source)),
        "single-group-inspect",
    )
    result = inspected.inspection

    assert inspected.status == "needs_review"
    assert result.blocking_issues == ()
    assert result.group_order == ("cohort",)
    assert tuple(image.subject_id for image in result.images) == expected_subjects
    assert {image.group for image in result.images} == {"cohort"}
    assert result.metric_kind.value == "alff"


def test_single_group_import_can_be_included_for_explicit_one_sample_analysis(
    service: ApplicationServices,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = _project(service, source_root, work_root)
    source = source_root / "single-group-analysis"
    subjects = ("sub-01", "sub-02", "sub-03")
    for index, subject_id in enumerate(subjects):
        _write_map(source / "cohort" / f"{subject_id}-zALFFMap.nii", offset=float(index))
    _write_map(source / "Mask.nii")

    inspected = service.external_imports.inspect_external_analysis_import(
        ExternalAnalysisImportInspectRequest(project_id=project_id, source_path=str(source)),
        "single-group-analysis-inspect",
    )
    included = service.external_imports.include_external_analysis_import(
        inspected.import_id,
        ExternalAnalysisImportIncludeRequest(
            expected_version=1,
            included_subject_ids=subjects,
            group_order=("cohort",),
            coordinate_space="unknown",
            acknowledge_unknown_processing=True,
            actor="researcher",
            reason="Explicit single-group inclusion for a one-sample test",
            mask_relative_path="Mask.nii",
        ),
        "single-group-analysis-include",
    )

    draft = StatisticsDraft(
        test=StatisticalTest.ONE_SAMPLE_T,
        one_sample_baseline=0.0,
        metric_artifact_type="external.alff.z_score",
        tail=Tail.TWO_SIDED,
        missing_value_policy=MissingValuePolicy.ERROR,
        correction_method="fdr",
        correction_parameters={"q_threshold": 0.05},
    )
    proposal, issues = service.statistics.prepare_work_statistical_design_proposal(
        external_import_id=included.import_id, project_id=project_id, draft=draft
    )

    assert issues == ()
    assert proposal is not None
    assert proposal.design.test is StatisticalTest.ONE_SAMPLE_T
    assert proposal.design.one_sample_baseline == 0.0
    assert proposal.design.group_order == ()
    assert proposal.design.subject_order == subjects
    assert all(image.group is None for image in proposal.design.images)

    verification_calls: list[bool] = []
    validate_external = service.statistics._validate_external_statistical_source

    def count_external_checks(request, *, verify_source_files):
        verification_calls.append(verify_source_files)
        return validate_external(request, verify_source_files=verify_source_files)

    monkeypatch.setattr(
        service.statistics, "_validate_external_statistical_source", count_external_checks
    )
    before_tables = _table_counts(service)
    before_files = _file_hashes(source_root)
    dry_run_proposal, dry_run_issues = service.statistics.dry_run_work_statistical_design_proposal(
        external_import_id=included.import_id, project_id=project_id, draft=draft
    )
    assert dry_run_issues == ()
    assert dry_run_proposal is not None
    assert verification_calls == [False, True, False]
    assert _table_counts(service) == before_tables
    assert _file_hashes(source_root) == before_files


def test_single_group_order_is_not_accepted_for_paired_layout(
    service: ApplicationServices, source_root: Path, work_root: Path
) -> None:
    project_id = _project(service, source_root, work_root)
    source = source_root / "paired-single-order"
    for condition in ("before", "after"):
        for subject_id in ("sub-01", "sub-02"):
            _write_map(source / condition / f"{subject_id}-zALFFMap.nii")

    inspected = service.external_imports.inspect_external_analysis_import(
        ExternalAnalysisImportInspectRequest(
            project_id=project_id,
            source_path=str(source),
            layout="paired_conditions",
        ),
        "paired-single-order-inspect",
    )

    try:
        service.external_imports.include_external_analysis_import(
            inspected.import_id,
            ExternalAnalysisImportIncludeRequest(
                expected_version=1,
                included_subject_ids=("sub-01", "sub-02"),
                group_order=("before",),
                coordinate_space="unknown",
                acknowledge_unknown_processing=True,
                actor="researcher",
                reason="Invalid one-condition paired inclusion",
            ),
            "paired-single-order-include",
        )
    except InputValidationError as error:
        assert error.code == "external_import_paired_condition_count"
    else:
        raise AssertionError("paired imports must retain exactly two conditions")


def test_scanned_subjects_accept_only_the_explicitly_chosen_statistical_method(
    tmp_path: Path,
) -> None:
    source = tmp_path / "single-group"
    for index in range(1, 5):
        _write_map(source / "cohort" / f"sub-{index:02d}-zALFFMap.nii", offset=float(index))
    inspected = inspect_external_metric_directory(source)
    subjects = tuple(
        DesignSubject(
            subject_id=image.subject_id,
            artifact_id=f"artifact-{image.subject_id}",
        )
        for image in inspected.images
    )
    qc = QcBinding(
        source=StatisticalQcSource.EXTERNAL_IMPORT,
        external_import_id="reviewed-import",
        external_import_hash="a" * 64,
    )

    one_sample, issues = assemble_statistical_design(
        choices=StatisticsChoices(
            test=StatisticalTest.ONE_SAMPLE_T,
            one_sample_baseline=0.0,
        ),
        subjects=subjects,
        covariate_values={},
        mask_artifact_id="reviewed-mask",
        revision_id="explicit-one-sample",
        qc=qc,
    )
    assert issues == ()
    assert one_sample is not None
    assert one_sample.test is StatisticalTest.ONE_SAMPLE_T
    assert one_sample.one_sample_baseline == 0.0
    assert one_sample.group_order == ()
    assert tuple(image.subject_id for image in one_sample.images) == tuple(
        image.subject_id for image in inspected.images
    )

    correlation, issues = assemble_statistical_design(
        choices=StatisticsChoices(
            test=StatisticalTest.CORRELATION,
            covariates=(CovariateChoice(name="age"),),
            target_predictor="age",
        ),
        subjects=subjects,
        covariate_values={"age": {f"sub-{index:02d}": float(index * 5) for index in range(1, 5)}},
        mask_artifact_id="reviewed-mask",
        revision_id="explicit-correlation",
        qc=qc,
    )
    assert issues == ()
    assert correlation is not None
    assert correlation.test is StatisticalTest.CORRELATION
    assert correlation.one_sample_baseline is None


def test_flat_layout_and_ambiguous_or_duplicate_subject_identity_are_blocked(
    tmp_path: Path,
) -> None:
    flat = tmp_path / "flat"
    _write_map(flat / "sub-01-zALFFMap.nii")
    flat_result = inspect_external_metric_directory(flat)
    assert flat_result.images == ()
    assert any("平铺根目录没有显式组标记" in issue for issue in flat_result.blocking_issues)

    ambiguous = tmp_path / "ambiguous"
    _write_map(ambiguous / "cohort" / "zALFFMap.nii")
    ambiguous_result = inspect_external_metric_directory(ambiguous)
    assert ambiguous_result.images == ()
    assert any(
        "无法从指标文件名提取明确受试者标识" in issue for issue in ambiguous_result.blocking_issues
    )

    metric_only = tmp_path / "metric-only"
    _write_map(metric_only / "cohort" / "ReHo.nii")
    metric_only_result = inspect_external_metric_directory(metric_only)
    assert metric_only_result.images == ()
    assert any(
        "无法从指标文件名提取明确受试者标识" in issue
        for issue in metric_only_result.blocking_issues
    )

    duplicate = tmp_path / "duplicate"
    _write_map(duplicate / "cohort" / "sub-01-zALFFMap.nii")
    _write_map(duplicate / "cohort" / "zALFFMap_sub-01.nii", offset=1)
    duplicate_result = inspect_external_metric_directory(duplicate)
    assert {image.subject_id for image in duplicate_result.images} == {"sub-01"}
    assert any("受试者标识重复" in issue for issue in duplicate_result.blocking_issues)
