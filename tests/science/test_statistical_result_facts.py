from __future__ import annotations

import json

import pytest

from neuroagent.application.contracts import StatisticalClusterRoiView, StatisticalResultDetailView
from neuroagent.application.reporting import build_statistical_reproducibility_report
from neuroagent.application.statistical_result_facts import (
    aggregate_roi,
    build_method_summary,
    localization_model_facts,
    render_result_readout,
    result_model_facts,
    verified_result_design,
)
from neuroagent.domain.fmri.results import StatisticalAtlasSelection, StatisticalResultManifest
from neuroagent.domain.fmri.statistics import (
    AnalysisImage,
    CovariateColumn,
    CovariateValue,
    GrfCorrection,
    StatisticalDesignRevision,
    StatisticalTest,
)

from .test_results_reporting import _artifacts, _design, _real_manifest


def _detail(design=None, manifest=None, atlas=None):
    design = design or _design()
    manifest = manifest or _real_manifest()
    report = build_statistical_reproducibility_report(
        manifest=manifest,
        design=design,
        correction=manifest.correction,
        qc_review_hash=design.qc_review_hash,
        environment_hash="d" * 64,
        plan_hash="e" * 64,
        localization_plan=atlas,
    )
    return StatisticalResultDetailView(
        result_id=manifest.result_id,
        project_id="project-a",
        run_id=manifest.run_id,
        design_revision_id=design.revision_id,
        mode=manifest.mode.value,
        non_scientific=manifest.non_scientific,
        non_scientific_reason=manifest.non_scientific_reason,
        bundle_hash=report.bundle_hash,
        artifact_count=len(manifest.artifacts),
        cluster_count=len(manifest.clusters),
        version=1,
        created_at="2026-10-10T00:00:00Z",
        manifest=manifest.model_dump(mode="json"),
        report_json=report.json_text,
        report_markdown=report.markdown,
    )


def _method_design(test):
    base = _design().model_dump(mode="python")
    subjects = tuple(f"sub-{index:02}" for index in range(1, 7))
    conditions = ("before", "after") if test == StatisticalTest.PAIRED_T else ()
    grouped = test in {
        StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
        StatisticalTest.ONE_WAY_ANOVA_ANCOVA,
    }
    groups = (
        ("ASD", "HC", "OTHER")
        if test == StatisticalTest.ONE_WAY_ANOVA_ANCOVA
        else ("ASD", "HC")
        if grouped
        else ()
    )
    if conditions:
        subjects = subjects[:3]
        images = tuple(
            AnalysisImage(
                subject_id=subject,
                artifact_id=f"map-{condition}-{subject}",
                group=None,
                condition=condition,
            )
            for condition in conditions
            for subject in subjects
        )
        contrast = (1.0, 0.0, 0.0, 0.0)
    else:
        images = tuple(
            AnalysisImage(
                subject_id=subject,
                artifact_id=f"map-{subject}",
                group=groups[index // (len(subjects) // len(groups))] if groups else None,
                condition=None,
            )
            for index, subject in enumerate(subjects)
        )
        contrast = (1.0, 1.0, 0.0) if len(groups) == 3 else (1.0, 0.0) if groups else (1.0,)
    covariates = ()
    if test in {StatisticalTest.CORRELATION, StatisticalTest.REGRESSION}:
        covariates = (
            CovariateColumn(
                name="predictor",
                centering="grand_mean",
                values=tuple(
                    CovariateValue(subject_id=subject, value=index)
                    for index, subject in enumerate(subjects)
                ),
            ),
        )
        contrast = (1.0, 0.0) if test == StatisticalTest.CORRELATION else (0.0, 1.0)
    base.update(
        test=test,
        subject_order=subjects,
        images=images,
        group_order=groups,
        condition_order=conditions,
        covariates=covariates,
        contrast=contrast,
        one_sample_baseline=10.0 if test == StatisticalTest.ONE_SAMPLE_T else None,
    )
    return StatisticalDesignRevision.model_validate(base)


@pytest.mark.parametrize("test", list(StatisticalTest))
def test_six_method_summaries_preserve_counts_degrees_of_freedom_and_direction(test):
    design = _method_design(test)
    values = _real_manifest().model_dump(mode="python")
    values.update(
        correction=None,
        artifacts=_artifacts(corrected=False),
        clusters=(),
        inference_status=None,
        significant_voxel_count=None,
    )
    detail = _detail(design, StatisticalResultManifest.model_validate(values))
    summary = build_method_summary(detail)
    assert summary.test == test.value
    assert summary.subject_count == (3 if test == StatisticalTest.PAIRED_T else 6)
    assert summary.image_count == 6
    if test == StatisticalTest.ONE_WAY_ANOVA_ANCOVA:
        assert summary.statistic_type == "F" and summary.df1 == 2 and summary.df2 == 3
        assert summary.positive_direction is None and summary.negative_direction is None
    elif test == StatisticalTest.PAIRED_T:
        assert summary.df1 == 2 and summary.positive_direction == "before > after"
    elif test == StatisticalTest.INDEPENDENT_TWO_SAMPLE_T:
        assert summary.df1 == 4 and summary.group_counts == {"ASD": 3, "HC": 3}
        assert summary.positive_direction == "ASD > HC" and summary.negative_direction == "ASD < HC"
    elif test == StatisticalTest.CORRELATION:
        assert summary.df1 == 4 and summary.statistic_type == "R"
    elif test == StatisticalTest.REGRESSION:
        assert summary.df1 == 4 and summary.positive_direction == "predictor 系数 > 0"
    else:
        assert summary.df1 == 5 and summary.positive_direction == "均值 > 10"
    encoded = json.dumps(summary.model_dump(mode="json"), ensure_ascii=False)
    assert "sub-01" not in encoded and '"values"' not in encoded


def test_unsupported_negative_regression_contrast_is_rejected_in_a_frozen_report():
    design = _method_design(StatisticalTest.REGRESSION).model_copy(update={"contrast": (0.0, -1.0)})
    values = _real_manifest().model_dump(mode="python")
    values.update(
        correction=None,
        artifacts=_artifacts(corrected=False),
        clusters=(),
        inference_status=None,
        significant_voxel_count=None,
    )
    with pytest.raises(ValueError, match="unit contrast"):
        build_method_summary(_detail(design, StatisticalResultManifest.model_validate(values)))


def test_actual_fdr_cutoff_is_distinct_from_q_and_evidence_drift_is_rejected():
    detail = _detail()
    summary = build_method_summary(
        detail,
        correction_evidence={
            "method": "fdr",
            "q_threshold": 0.05,
            "df1": 2,
            "df2": [],
            "actual_p_threshold": 0.0002,
        },
        correction_evidence_status="verified",
    )
    assert summary.q_threshold == 0.05 and summary.actual_p_threshold == 0.0002
    with pytest.raises(ValueError, match="differs from"):
        build_method_summary(
            detail, correction_evidence={"method": "fdr", "q_threshold": 0.1, "df1": 2, "df2": []}
        )
    with pytest.raises(ValueError, match="bundle hash"):
        verified_result_design(detail.model_copy(update={"report_json": detail.report_json + " "}))


@pytest.mark.parametrize(
    "space,status",
    [
        ("MNI", "atlas_not_selected"),
        ("native", "coordinate_space_mismatch"),
        ("unknown", "coordinate_space_unknown"),
    ],
)
def test_facts_distinguish_known_native_space_from_unknown(space, status):
    manifest = _real_manifest().model_copy(
        update={
            "clusters": tuple(
                item.model_copy(update={"coordinate_space": space})
                for item in _real_manifest().clusters
            )
        }
    )
    detail = _detail(manifest=manifest)
    detail = detail.model_copy(update={"method_summary": build_method_summary(detail)})
    facts = result_model_facts(detail)
    assert facts["localization_status"] == status
    assert "report_json" not in facts and "manifest" not in facts
    assert "sub-01" not in json.dumps(facts)
    assert json.loads(detail.report_json)["inference_summary"]["localization_applicable"] is (
        space == "MNI"
    )


def test_empty_corrected_result_is_normal_even_with_unknown_space_or_no_atlas():
    values = _real_manifest().model_dump(mode="python")
    values.update(clusters=(), inference_status=None, significant_voxel_count=None)
    detail = _detail(manifest=StatisticalResultManifest.model_validate(values))
    detail = detail.model_copy(update={"method_summary": build_method_summary(detail)})
    facts = result_model_facts(detail)
    assert (
        facts["localization_status"] == "no_significant_voxels"
        and facts["significant_voxel_count"] == 0
    )
    text = render_result_readout(facts)
    assert "0 个" in text and "没有脑区可定位" in text
    assert "空间尚未确认" not in text and "不能证明两组等价" in text


def test_localization_projection_reports_truncation_and_drops_unregistered_fields():
    facts = localization_model_facts(
        {
            "status": "localized",
            "localization_method": "inverse affine nearest neighbor",
            "raw_report": "private source data",
            "clusters": [
                {
                    "cluster_id": str(index),
                    "distance_to_voxel_center_mm": 0.5,
                    "source_path": "private source path",
                    "coverage": [
                        {"label": str(region), "proportion": 1 / 11, "private": "private"}
                        for region in range(11)
                    ],
                }
                for index in range(21)
            ],
        }
    )
    assert facts["cluster_count"] == 21 and facts["clusters_truncated"]
    assert facts["localization_method"] == "inverse affine nearest neighbor"
    assert facts["clusters"][0]["distance_to_voxel_center_mm"] == 0.5
    assert len(facts["clusters"]) == 20
    assert all(
        item["coverage_count"] == 11 and item["coverage_truncated"] for item in facts["clusters"]
    )
    assert all(len(item["coverage"]) == 10 for item in facts["clusters"])
    assert "private" not in json.dumps(facts)


@pytest.mark.parametrize("source", ["provided", "header", "estimated"])
def test_grf_method_facts_preserve_actual_smoothness_and_cluster_threshold(source):
    correction = GrfCorrection(
        method="grf",
        voxel_p_threshold=0.001,
        cluster_p_threshold=0.05,
        two_tailed=True,
        mask_artifact_id="mask-1",
        statistic_type="T",
        df1=2,
        df2=None,
        smoothness_mode="provided_dlh" if source == "provided" else "dpabi_header_or_estimate",
        smoothness_dlh=0.05 if source == "provided" else None,
    )
    detail = _detail(manifest=_real_manifest().model_copy(update={"correction": correction}))
    evidence = {
        **correction.model_dump(mode="json"),
        "df2": [],
        "tail": "two_sided",
        "smoothness_source": source,
        "effective_smoothness_dlh": 0.05,
        "effective_fwhm": [] if source == "provided" else [4, 5, 6],
        "effective_mask_voxel_count": 100,
        "minimum_cluster_size": 7,
        "cluster_connectivity": 26,
    }
    evidence["smoothness_dlh"] = 0.05 if source == "provided" else []
    summary = build_method_summary(
        detail, correction_evidence=evidence, correction_evidence_status="verified"
    )
    assert summary.minimum_cluster_size == 7 and summary.smoothness_source == source
    assert summary.effective_smoothness_dlh == 0.05 and summary.cluster_connectivity == 26
    assert summary.effective_fwhm == (None if source == "provided" else (4, 5, 6))
    for changed in (
        {"tail": "one_sided_negative"},
        {"cluster_connectivity": 6},
        {"effective_smoothness_dlh": float("nan")},
        {"effective_fwhm": [4, 5, -6]},
        {"smoothness_source": "estimated" if source == "provided" else "provided"},
    ):
        with pytest.raises(ValueError):
            build_method_summary(detail, correction_evidence={**evidence, **changed})


def test_frozen_atlas_is_reported_as_pending_until_the_query_verifies_it():
    atlas = StatisticalAtlasSelection(
        atlas_id="aal", atlas_name="AAL", atlas_content_hash="f" * 64, coordinate_space="MNI"
    )
    detail = _detail(atlas=atlas)
    detail = detail.model_copy(update={"method_summary": build_method_summary(detail)})
    assert result_model_facts(detail)["localization_status"] == "pending_atlas_verification"


@pytest.mark.parametrize("space", ["MNI", "native", "unknown"])
def test_deterministic_readout_distinguishes_descriptive_uncorrected_results_and_space(space):
    values = _real_manifest().model_dump(mode="python")
    values.update(
        correction=None,
        artifacts=_artifacts(corrected=False),
        inference_status=None,
        significant_voxel_count=None,
    )
    values["clusters"] = tuple(
        {
            **item,
            "source_map_artifact_id": "artifact-uncorrected_statistical_map",
            "coordinate_space": space,
        }
        for item in values["clusters"]
    )
    detail = _detail(manifest=StatisticalResultManifest.model_validate(values))
    detail = detail.model_copy(update={"method_summary": build_method_summary(detail)})
    facts = result_model_facts(detail)
    assert facts["inference_status"] == "uncorrected_descriptive"
    text = render_result_readout(facts)
    assert "未作多重比较校正" in text
    assert ("结果空间已知" in text) is (space == "native")
    assert ("空间尚未确认" in text) is (space == "unknown")


def test_deterministic_f_grf_readout_never_assigns_group_direction():
    design = _method_design(StatisticalTest.ONE_WAY_ANOVA_ANCOVA)
    values = _real_manifest().model_dump(mode="python")
    values.update(
        correction=None,
        artifacts=_artifacts(corrected=False),
        clusters=(),
        inference_status=None,
        significant_voxel_count=None,
    )
    detail = _detail(design=design, manifest=StatisticalResultManifest.model_validate(values))
    method = build_method_summary(detail).model_dump(mode="json")
    method.update(correction_method="grf", voxel_p_threshold=0.001, cluster_p_threshold=0.05)
    text = render_result_readout(
        {
            "status": "succeeded",
            "non_scientific": False,
            "method": method,
            "inference_status": "significant_voxels",
            "significant_voxel_count": 12,
            "cluster_count": 2,
        }
    )
    assert "总体 F 检验不表示哪组更高" in text and "正值" not in text
    assert "GRF：体素 p=0.001，簇 p=0.05" in text  # noqa: RUF001
    assert "12 个显著体素，2 个簇" in text  # noqa: RUF001
    assert "暂不可核验" in render_result_readout({"status": "method_summary_unavailable"})
    assert "非科学模拟" in render_result_readout({"status": "succeeded", "non_scientific": True})


def test_roi_aggregates_preserve_pair_conditions_and_exclude_subject_values():
    roi = StatisticalClusterRoiView(
        result_id="r",
        result_bundle_hash="a" * 64,
        cluster_id="c",
        status="descriptive",
        voxel_count=8,
        rows=tuple(
            {
                "subject_id": f"private-{index}",
                "group": group,
                "condition": condition,
                "mean_value": value,
            }
            for index, (group, condition, value) in enumerate(
                [
                    ("ASD", "before", 1),
                    ("ASD", "before", 3),
                    ("ASD", "after", 2),
                    ("ASD", "after", 4),
                    ("HC", None, 10),
                ]
            )
        ),
    )
    facts = aggregate_roi(roi)
    assert facts["groups"][0]["mean"] == 3 and facts["groups"][0]["count"] == 2
    assert facts["groups"][0]["sample_standard_deviation"] == pytest.approx(2**0.5)
    assert facts["groups"][-1]["sample_standard_deviation"] is None
    encoded = json.dumps(facts)
    assert "private-" not in encoded and "subject_id" not in encoded and "mean_value" not in encoded
