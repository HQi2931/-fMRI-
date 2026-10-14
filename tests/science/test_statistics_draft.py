"""Design assembly: server-sourced data plus explicit method choices.

The property under test is provenance.  A model may name a column, but it must
never be able to fabricate a subgroup membership or a covariate number; the
assembler only accepts values the server already holds, and it reports blocking
issues instead of raising so a conversational flow can explain them.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import pytest

from neuroagent.application.statistics_draft import (
    ArtifactRef,
    CovariateChoice,
    DesignSubject,
    QcBinding,
    StatisticsChoices,
    StatisticsDraft,
    assemble_statistical_design,
    collect_design_sources,
    merge_statistics_draft,
    render_statistics_draft_review,
    statistics_draft_hash,
    statistics_draft_issues,
)
from neuroagent.domain.fmri.statistics import (
    Centering,
    MissingValuePolicy,
    StatisticalQcSource,
    StatisticalTest,
    Tail,
)

_REVIEW = QcBinding(
    source=StatisticalQcSource.APPROVED_REVIEW,
    qc_review_revision_id="review-1",
    qc_review_hash="a" * 64,
)


def _subjects() -> tuple[DesignSubject, ...]:
    return (
        DesignSubject(subject_id="sub-01", artifact_id="art-01", group="control"),
        DesignSubject(subject_id="sub-02", artifact_id="art-02", group="control"),
        DesignSubject(subject_id="sub-03", artifact_id="art-03", group="control"),
        DesignSubject(subject_id="sub-04", artifact_id="art-04", group="patient"),
        DesignSubject(subject_id="sub-05", artifact_id="art-05", group="patient"),
        DesignSubject(subject_id="sub-06", artifact_id="art-06", group="patient"),
    )


def _covariates() -> dict[str, dict[str, float | None]]:
    return {
        "age": {
            "sub-01": 24.0,
            "sub-02": 27.0,
            "sub-03": 31.0,
            "sub-04": 41.0,
            "sub-05": 46.0,
            "sub-06": 52.0,
        }
    }


def _assemble(**overrides: object):
    arguments: dict[str, object] = {
        "choices": StatisticsChoices(
            test=StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
            grouping_column="group",
            group_order=("control", "patient"),
            tail=Tail.TWO_SIDED,
        ),
        "subjects": _subjects(),
        "covariate_values": {},
        "mask_artifact_id": "mask-1",
        "revision_id": "design-1",
        "qc": _REVIEW,
    }
    arguments.update(overrides)
    return assemble_statistical_design(**arguments)  # type: ignore[arg-type]


def test_two_sample_design_is_assembled_with_the_frozen_order() -> None:
    design, issues = _assemble()

    assert issues == ()
    assert design is not None
    assert design.subject_order == ("sub-01", "sub-02", "sub-03", "sub-04", "sub-05", "sub-06")
    assert [image.artifact_id for image in design.images] == [
        "art-01",
        "art-02",
        "art-03",
        "art-04",
        "art-05",
        "art-06",
    ]
    # The canonical first-column contrast is what the DPABI helper requires.
    assert design.contrast == (1.0, 0.0)
    assert design.group_order == ("control", "patient")
    assert design.one_sample_baseline is None


def test_group_order_comes_from_the_choice_not_from_the_data() -> None:
    """Reversing the groups must flip the contrast direction, not the data."""

    design, issues = _assemble(
        choices=StatisticsChoices(
            test=StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
            grouping_column="group",
            group_order=("patient", "control"),
        )
    )

    assert issues == ()
    assert design is not None
    assert design.group_order == ("patient", "control")
    assert design.contrast == (1.0, 0.0)


def test_covariate_values_are_aligned_to_the_subject_order() -> None:
    design, issues = _assemble(
        choices=StatisticsChoices(
            test=StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
            grouping_column="group",
            group_order=("control", "patient"),
            covariates=(CovariateChoice(name="age"),),
        ),
        covariate_values=_covariates(),
    )

    assert issues == ()
    assert design is not None
    column = design.covariates[0]
    assert column.name == "age"
    assert column.centering is Centering.GRAND_MEAN
    assert [value.subject_id for value in column.values] == list(design.subject_order)
    assert [value.value for value in column.values] == [24.0, 27.0, 31.0, 41.0, 46.0, 52.0]


def test_an_unregistered_covariate_column_is_a_blocking_issue() -> None:
    """A model cannot reference a column that does not exist in the data."""

    design, issues = _assemble(
        choices=StatisticsChoices(
            test=StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
            grouping_column="group",
            covariates=(CovariateChoice(name="invented_biomarker"),),
        )
    )

    assert design is None
    assert [issue.code for issue in issues] == ["covariate_column_missing"]
    assert "invented_biomarker" in issues[0].message


def test_a_missing_covariate_value_blocks_rather_than_defaulting() -> None:
    values = _covariates()
    values["age"]["sub-03"] = None

    design, issues = _assemble(
        choices=StatisticsChoices(
            test=StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
            grouping_column="group",
            covariates=(CovariateChoice(name="age"),),
        ),
        covariate_values=values,
    )

    assert design is None
    assert [issue.code for issue in issues] == ["covariate_value_missing"]


def test_explicit_exclusion_policy_still_requires_the_subject_to_be_removed() -> None:
    """Choosing the exclusion policy must not silently drop a subject row."""

    values = _covariates()
    values["age"]["sub-03"] = None

    design, issues = _assemble(
        choices=StatisticsChoices(
            test=StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
            grouping_column="group",
            covariates=(CovariateChoice(name="age"),),
            missing_value_policy=MissingValuePolicy.EXCLUDE_EXPLICITLY,
        ),
        covariate_values=values,
    )

    assert design is None
    assert [issue.code for issue in issues] == ["covariate_value_missing"]


def test_two_sample_without_a_grouping_column_is_blocked() -> None:
    design, issues = _assemble(
        choices=StatisticsChoices(test=StatisticalTest.INDEPENDENT_TWO_SAMPLE_T)
    )

    assert design is None
    assert "grouping_column_required" in {issue.code for issue in issues}


def test_a_single_group_cannot_be_assembled_as_a_two_sample_test() -> None:
    design, issues = _assemble(
        choices=StatisticsChoices(
            test=StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
            grouping_column="group",
        ),
        subjects=tuple(
            DesignSubject(
                subject_id=f"sub-{index:02d}", artifact_id=f"art-{index:02d}", group="only"
            )
            for index in range(1, 4)
        ),
    )

    assert design is None
    assert "group_order_unsupported" in {issue.code for issue in issues}


def test_unlabelled_subjects_are_blocked() -> None:
    subjects = (
        DesignSubject(subject_id="sub-01", artifact_id="art-01", group="control"),
        DesignSubject(subject_id="sub-02", artifact_id="art-02", group=None),
        DesignSubject(subject_id="sub-03", artifact_id="art-03", group="patient"),
    )

    design, issues = _assemble(subjects=subjects)

    assert design is None
    assert "group_label_missing" in {issue.code for issue in issues}


def test_one_sample_design_requires_an_explicit_baseline() -> None:
    design, issues = _assemble(
        choices=StatisticsChoices(test=StatisticalTest.ONE_SAMPLE_T),
        subjects=tuple(
            DesignSubject(subject_id=f"sub-{index:02d}", artifact_id=f"art-{index:02d}")
            for index in range(1, 4)
        ),
    )
    assert design is None
    assert [issue.code for issue in issues] == ["baseline_required"]


def test_one_sample_design_carries_the_baseline_and_canonical_contrast() -> None:
    design, issues = _assemble(
        choices=StatisticsChoices(test=StatisticalTest.ONE_SAMPLE_T, one_sample_baseline=0.0),
        subjects=tuple(
            DesignSubject(subject_id=f"sub-{index:02d}", artifact_id=f"art-{index:02d}")
            for index in range(1, 4)
        ),
    )

    assert issues == ()
    assert design is not None
    assert design.one_sample_baseline == 0.0
    assert design.contrast == (1.0,)
    assert design.group_order == ()
    assert all(image.group is None for image in design.images)


def test_paired_design_is_ordered_by_condition_then_subject() -> None:
    subjects = tuple(
        DesignSubject(
            subject_id=f"sub-{index:02d}",
            artifact_id=f"art-{condition}-{index:02d}",
            condition=condition,
        )
        for index in range(1, 4)
        for condition in ("post", "pre")
    )

    design, issues = _assemble(
        choices=StatisticsChoices(
            test=StatisticalTest.PAIRED_T,
            condition_column="session",
            condition_order=("pre", "post"),
        ),
        subjects=subjects,
    )

    assert issues == ()
    assert design is not None
    assert design.condition_order == ("pre", "post")
    assert [(image.subject_id, image.condition) for image in design.images] == [
        ("sub-01", "pre"),
        ("sub-02", "pre"),
        ("sub-03", "pre"),
        ("sub-01", "post"),
        ("sub-02", "post"),
        ("sub-03", "post"),
    ]
    assert design.subject_order == ("sub-01", "sub-02", "sub-03")


def test_paired_design_without_an_explicit_condition_order_is_blocked() -> None:
    subjects = tuple(
        DesignSubject(subject_id="sub-01", artifact_id=f"art-{condition}", condition=condition)
        for condition in ("pre", "post")
    )

    design, issues = _assemble(
        choices=StatisticsChoices(test=StatisticalTest.PAIRED_T, condition_column="session"),
        subjects=subjects,
    )

    assert design is None
    assert "condition_order_unsupported" in {issue.code for issue in issues}


def test_empty_and_duplicated_subject_lists_are_blocked() -> None:
    design, issues = _assemble(subjects=())
    assert design is None
    assert [issue.code for issue in issues] == ["no_subjects"]

    duplicated = (
        DesignSubject(subject_id="sub-01", artifact_id="art-01", group="control"),
        DesignSubject(subject_id="sub-01", artifact_id="art-02", group="patient"),
    )
    design, issues = _assemble(subjects=duplicated)
    assert design is None
    assert [issue.code for issue in issues] == ["duplicate_subject"]


def test_regression_requires_explicit_predictors_instead_of_guessing() -> None:
    design, issues = _assemble(
        choices=StatisticsChoices(test=StatisticalTest.REGRESSION),
        subjects=tuple(
            DesignSubject(subject_id=f"sub-{index:02d}", artifact_id=f"art-{index:02d}")
            for index in range(1, 4)
        ),
    )

    assert design is None
    assert {issue.code for issue in issues} == {
        "predictor_required",
        "target_predictor_required",
    }


def test_domain_rule_violations_surface_as_issues_not_exceptions() -> None:
    """Within-group centering is only legal for a two-sample test; the domain
    rejects it elsewhere and the caller must receive an issue instead of a raise."""

    design, issues = _assemble(
        choices=StatisticsChoices(
            test=StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
            grouping_column="group",
            covariates=(CovariateChoice(name="age", centering=Centering.WITHIN_GROUP),),
        ),
        covariate_values=_covariates(),
        subjects=(
            DesignSubject(subject_id="sub-01", artifact_id="art-01", group="control"),
            DesignSubject(subject_id="sub-02", artifact_id="art-02", group="control"),
            DesignSubject(subject_id="sub-03", artifact_id="art-03", group="patient"),
            DesignSubject(subject_id="sub-04", artifact_id="art-04", group="patient"),
        ),
    )

    # Within-group centering on a two-sample test is legal, so this assembles.
    assert issues == ()
    assert design is not None

    # A collinear covariate is rejected by the domain and reported as an issue.
    design, issues = _assemble(
        choices=StatisticsChoices(
            test=StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
            grouping_column="group",
            covariates=(CovariateChoice(name="constant"),),
        ),
        covariate_values={
            "constant": {subject.subject_id: 7.0 for subject in _subjects()},
        },
    )
    assert design is None
    assert [issue.code for issue in issues] == ["design_rejected_by_domain"]
    assert "full column rank" in issues[0].message


def test_qc_binding_is_recorded_verbatim() -> None:
    design, issues = _assemble(
        qc=QcBinding(
            source=StatisticalQcSource.SKIPPED,
            qc_skip_id="skip-1",
            qc_skip_hash="b" * 64,
            qc_source_run_id="run-1",
        )
    )

    assert issues == ()
    assert design is not None
    assert design.qc_source is StatisticalQcSource.SKIPPED
    assert design.qc_skip_id == "skip-1"
    assert design.qc_source_run_id == "run-1"
    assert design.qc_review_revision_id is None


def test_approved_review_without_a_hash_is_reported_by_the_domain() -> None:
    design, issues = _assemble(
        qc=QcBinding(source=StatisticalQcSource.APPROVED_REVIEW, qc_review_revision_id="r1")
    )

    assert design is None
    assert [issue.code for issue in issues] == ["design_rejected_by_domain"]


def test_choices_reject_unknown_fields() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        StatisticsChoices.model_validate({"test": "one_sample_t", "invented_field": "value"})


# ---------------------------------------------------------------------------
# Source resolution: the QC decision supplies the order, not the file system.
# ---------------------------------------------------------------------------


def _artifacts() -> tuple[ArtifactRef, ...]:
    return (
        ArtifactRef(artifact_id="alff-03", artifact_type="metric.alff", subject_id="sub-03"),
        ArtifactRef(artifact_id="alff-01", artifact_type="metric.alff", subject_id="sub-01"),
        ArtifactRef(artifact_id="alff-02", artifact_type="metric.alff", subject_id="sub-02"),
        ArtifactRef(artifact_id="mask-1", artifact_type="mask.brain"),
        ArtifactRef(
            artifact_id="ts-01", artifact_type="functional_timeseries", subject_id="sub-01"
        ),
    )


def test_sources_follow_the_qc_order_not_the_artifact_order() -> None:
    """The artifact list above is deliberately out of order."""

    sources, issues = collect_design_sources(
        artifacts=_artifacts(),
        included_subject_ids=("sub-02", "sub-03", "sub-01"),
        metric_artifact_type="metric.alff",
        mask_artifact_type="mask.brain",
    )

    assert issues == ()
    assert sources is not None
    assert sources.subject_order == ("sub-02", "sub-03", "sub-01")
    assert [subject.artifact_id for subject in sources.subjects] == [
        "alff-02",
        "alff-03",
        "alff-01",
    ]
    assert sources.mask_artifact_id == "mask-1"


def test_sources_carry_the_label_column_values() -> None:
    sources, issues = collect_design_sources(
        artifacts=_artifacts(),
        included_subject_ids=("sub-01", "sub-02", "sub-03"),
        metric_artifact_type="metric.alff",
        mask_artifact_type="mask.brain",
        label_column="group",
        label_values={"sub-01": "control", "sub-02": "patient", "sub-03": "patient"},
    )

    assert issues == ()
    assert sources is not None
    assert [subject.group for subject in sources.subjects] == ["control", "patient", "patient"]


def test_a_subject_without_a_label_blocks_the_design() -> None:
    sources, issues = collect_design_sources(
        artifacts=_artifacts(),
        included_subject_ids=("sub-01", "sub-02"),
        metric_artifact_type="metric.alff",
        mask_artifact_type="mask.brain",
        label_column="group",
        label_values={"sub-01": "control"},
    )

    assert sources is None
    assert [issue.code for issue in issues] == ["label_value_missing"]
    assert "sub-02" in issues[0].message


def test_an_excluded_subject_is_not_silently_included() -> None:
    """Only the frozen inclusion set may appear in the design."""

    sources, issues = collect_design_sources(
        artifacts=_artifacts(),
        included_subject_ids=("sub-01",),
        metric_artifact_type="metric.alff",
        mask_artifact_type="mask.brain",
    )

    assert issues == ()
    assert sources is not None
    assert sources.subject_order == ("sub-01",)


def test_a_missing_metric_artifact_blocks_and_names_the_subject() -> None:
    sources, issues = collect_design_sources(
        artifacts=_artifacts(),
        included_subject_ids=("sub-01", "sub-99"),
        metric_artifact_type="metric.alff",
        mask_artifact_type="mask.brain",
    )

    assert sources is None
    assert [issue.code for issue in issues] == ["missing_metric_artifact"]
    assert "sub-99" in issues[0].message


def test_an_ambiguous_metric_artifact_blocks_rather_than_picking_one() -> None:
    artifacts = (
        *_artifacts(),
        ArtifactRef(artifact_id="alff-01b", artifact_type="metric.alff", subject_id="sub-01"),
    )

    sources, issues = collect_design_sources(
        artifacts=artifacts,
        included_subject_ids=("sub-01",),
        metric_artifact_type="metric.alff",
        mask_artifact_type="mask.brain",
    )

    assert sources is None
    assert [issue.code for issue in issues] == ["ambiguous_metric_artifact"]


def test_mask_artifacts_must_be_present_and_unambiguous() -> None:
    sources, issues = collect_design_sources(
        artifacts=(
            ArtifactRef(artifact_id="alff-01", artifact_type="metric.alff", subject_id="sub-01"),
        ),
        included_subject_ids=("sub-01",),
        metric_artifact_type="metric.alff",
        mask_artifact_type="mask.brain",
    )
    assert sources is None
    assert [issue.code for issue in issues] == ["mask_artifact_missing"]

    ambiguous = (
        *_artifacts(),
        ArtifactRef(artifact_id="mask-2", artifact_type="mask.brain"),
    )
    sources, issues = collect_design_sources(
        artifacts=ambiguous,
        included_subject_ids=("sub-01",),
        metric_artifact_type="metric.alff",
        mask_artifact_type="mask.brain",
    )
    assert sources is None
    assert [issue.code for issue in issues] == ["ambiguous_mask_artifact"]


def test_empty_and_duplicated_inclusion_sets_are_blocked() -> None:
    sources, issues = collect_design_sources(
        artifacts=_artifacts(),
        included_subject_ids=(),
        metric_artifact_type="metric.alff",
        mask_artifact_type="mask.brain",
    )
    assert sources is None
    assert [issue.code for issue in issues] == ["no_included_subjects"]

    sources, issues = collect_design_sources(
        artifacts=_artifacts(),
        included_subject_ids=("sub-01", "sub-01"),
        metric_artifact_type="metric.alff",
        mask_artifact_type="mask.brain",
    )
    assert sources is None
    assert [issue.code for issue in issues] == ["duplicate_included_subject"]


def test_resolver_output_feeds_the_assembler_end_to_end() -> None:
    """The two halves compose: resolved sources plus choices make a real design."""

    sources, source_issues = collect_design_sources(
        artifacts=_artifacts(),
        included_subject_ids=("sub-01", "sub-02", "sub-03"),
        metric_artifact_type="metric.alff",
        mask_artifact_type="mask.brain",
        label_column="group",
        label_values={"sub-01": "control", "sub-02": "patient", "sub-03": "patient"},
    )
    assert source_issues == ()
    assert sources is not None

    design, design_issues = assemble_statistical_design(
        choices=StatisticsChoices(
            test=StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
            grouping_column="group",
            group_order=("control", "patient"),
        ),
        subjects=sources.subjects,
        covariate_values={},
        mask_artifact_id=sources.mask_artifact_id,
        revision_id="design-e2e",
        qc=_REVIEW,
    )

    assert design_issues == ()
    assert design is not None
    assert design.subject_order == ("sub-01", "sub-02", "sub-03")
    assert design.mask_artifact_id == "mask-1"
    assert design.group_order == ("control", "patient")
    assert design.contrast == (1.0, 0.0)


# ---------------------------------------------------------------------------
# Multi-turn draft: choices accumulate, server bindings cannot be injected.
# ---------------------------------------------------------------------------


def test_draft_accumulates_choices_across_turns() -> None:
    draft = merge_statistics_draft(
        StatisticsDraft(),
        {"test": "independent_two_sample_t", "metric_artifact_type": "metric.alff.raw"},
    )
    draft = merge_statistics_draft(draft, {"grouping_column": "group"})
    draft = merge_statistics_draft(
        draft,
        {
            "group_order": ["control", "patient"],
            "tail": "two_sided",
            "missing_value_policy": "error",
        },
    )
    draft = merge_statistics_draft(
        draft,
        {
            "correction_method": "fdr",
            "correction_parameters": {"q_threshold": 0.05},
        },
    )

    assert draft.test is StatisticalTest.INDEPENDENT_TWO_SAMPLE_T
    assert draft.metric_artifact_type == "metric.alff.raw"
    assert draft.grouping_column == "group"
    assert draft.group_order == ["control", "patient"]
    assert draft.correction_parameters == {"q_threshold": 0.05}
    assert statistics_draft_issues(draft) == ()


def test_a_model_patch_cannot_claim_server_owned_bindings() -> None:
    """A model must not be able to bind a design to a run, QC decision or manifest."""

    draft = merge_statistics_draft(
        StatisticsDraft(),
        {
            "test": "one_sample_t",
            "source_run_id": "run-forged",
            "qc_review_revision_id": "review-forged",
            "qc_review_hash": "f" * 64,
            "input_manifest_hash": "e" * 64,
            "confirmed_hash": "d" * 64,
        },
    )

    assert draft.test is StatisticalTest.ONE_SAMPLE_T
    assert draft.source_run_id is None
    assert draft.qc_review_revision_id is None
    assert draft.qc_review_hash is None
    assert draft.input_manifest_hash is None
    assert draft.confirmed_hash is None


def test_an_unknown_patch_field_is_ignored_rather_than_stored() -> None:
    draft = merge_statistics_draft(StatisticsDraft(), {"invented_field": "value"})

    assert "invented_field" not in draft.model_dump()


def test_draft_issues_name_what_is_still_missing() -> None:
    issues = statistics_draft_issues(StatisticsDraft())
    assert "检验类型尚未选择" in issues
    assert "指标产物类型尚未选择" in issues

    two_sample = statistics_draft_issues(
        StatisticsDraft(
            test=StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
            metric_artifact_type="metric.alff.raw",
        )
    )
    assert "分组列尚未指定" in two_sample
    assert "分组顺序尚未明确给出两个分组" in two_sample

    paired = statistics_draft_issues(
        StatisticsDraft(test=StatisticalTest.PAIRED_T, metric_artifact_type="metric.alff.raw")
    )
    assert "条件列尚未指定" in paired

    one_sample = statistics_draft_issues(
        StatisticsDraft(test=StatisticalTest.ONE_SAMPLE_T, metric_artifact_type="metric.alff.raw")
    )
    assert "单样本检验基线尚未给出" in one_sample


def test_correction_parameters_without_a_method_are_refused() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="校正参数必须与校正方法一起提供"):
        StatisticsDraft(correction_parameters={"q_threshold": 0.05})


def test_draft_hash_ignores_the_confirmation_seal() -> None:
    base = StatisticsDraft(
        test=StatisticalTest.ONE_SAMPLE_T, metric_artifact_type="metric.alff.raw"
    )
    sealed = base.model_copy(update={"confirmed_hash": "a" * 64})

    assert statistics_draft_hash(base) == statistics_draft_hash(sealed)
    # But any real choice change must move the hash.
    assert statistics_draft_hash(base) != statistics_draft_hash(
        base.model_copy(update={"tail": Tail.TWO_SIDED})
    )


def test_draft_review_states_that_identifiers_come_from_the_server() -> None:
    rendered = render_statistics_draft_review(
        StatisticsDraft(
            test=StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
            metric_artifact_type="metric.alff.raw",
            grouping_column="group",
            group_order=["control", "patient"],
            tail=Tail.TWO_SIDED,
            covariates=[CovariateChoice(name="age", centering=Centering.GRAND_MEAN)],
            missing_value_policy=MissingValuePolicy.ERROR,
            correction_method="fdr",
            correction_parameters={"q_threshold": 0.05},
        )
    )

    assert "independent_two_sample_t" in rendered
    assert "group（顺序：control、patient）" in rendered
    assert "缺失值处理：error" in rendered
    assert "age（grand_mean）" in rendered
    assert "fdr（q_threshold=0.05）" in rendered
    # The review must be explicit that the model did not choose the data binding.
    assert "由服务端" in rendered


def test_draft_review_shows_an_uncorrected_design_explicitly() -> None:
    rendered = render_statistics_draft_review(
        StatisticsDraft(
            test=StatisticalTest.ONE_SAMPLE_T,
            one_sample_baseline=0.0,
            missing_value_policy=MissingValuePolicy.ERROR,
            correction_method="none",
        )
    )

    assert "- 协变量：无" in rendered
    assert "- 多重比较校正：未校正" in rendered
    assert "- 缺失值处理：error" in rendered
    assert "检验基线：0.0" in rendered


@pytest.mark.parametrize(
    ("method", "parameters"),
    [
        ("fdr", {"q_threshold": 0.05, "invented": 1}),
        ("grf", {"voxel_p_threshold": 0.001, "cluster_p_threshold": 0.05, "invented": 1}),
    ],
)
def test_draft_rejects_unrecognized_correction_parameters(method, parameters) -> None:
    issues = statistics_draft_issues(
        StatisticsDraft(
            test=StatisticalTest.ONE_SAMPLE_T,
            one_sample_baseline=0,
            metric_artifact_type="metric.alff.raw",
            tail=Tail.TWO_SIDED,
            missing_value_policy=MissingValuePolicy.ERROR,
            correction_method=method,
            correction_parameters=parameters,
        )
    )

    assert any("未识别参数" in issue for issue in issues)


def test_merged_draft_feeds_choices_for_assembly() -> None:
    """The draft is a transport for choices, not a second source of truth."""

    draft = merge_statistics_draft(
        StatisticsDraft(),
        {
            "test": "independent_two_sample_t",
            "metric_artifact_type": "metric.alff.raw",
            "grouping_column": "group",
            "group_order": ["control", "patient"],
            "tail": "two_sided",
            "missing_value_policy": "error",
            "correction_method": "none",
        },
    )
    assert statistics_draft_issues(draft) == ()

    choices = StatisticsChoices(
        test=draft.test,
        grouping_column=draft.grouping_column,
        group_order=tuple(draft.group_order),
        tail=draft.tail or Tail.TWO_SIDED,
    )
    subjects = (
        DesignSubject(subject_id="sub-01", artifact_id="art-01", group="control"),
        DesignSubject(subject_id="sub-02", artifact_id="art-02", group="control"),
        DesignSubject(subject_id="sub-03", artifact_id="art-03", group="patient"),
        DesignSubject(subject_id="sub-04", artifact_id="art-04", group="patient"),
    )
    design, issues = assemble_statistical_design(
        choices=choices,
        subjects=subjects,
        covariate_values={},
        mask_artifact_id="mask-1",
        revision_id="design-from-draft",
        qc=_REVIEW,
    )

    assert issues == ()
    assert design is not None
    assert design.group_order == ("control", "patient")
