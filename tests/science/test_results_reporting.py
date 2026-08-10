from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from neuroagent.application import build_statistical_reproducibility_report
from neuroagent.domain.fmri import (
    AnalysisImage,
    ClusterRecord,
    FdrCorrection,
    MissingValuePolicy,
    RegisteredArtifactMetadata,
    StatisticalArtifactRole,
    StatisticalDesignRevision,
    StatisticalMapType,
    StatisticalResultManifest,
    StatisticalResultMode,
    StatisticalTest,
    Tail,
)


def _design() -> StatisticalDesignRevision:
    subjects = ("sub-01", "sub-02", "sub-03")
    return StatisticalDesignRevision(
        revision_id="stats-result-1",
        test=StatisticalTest.ONE_SAMPLE_T,
        subject_order=subjects,
        images=tuple(
            AnalysisImage(
                subject_id=subject_id,
                artifact_id=f"metric-{subject_id}",
                group=None,
                condition=None,
            )
            for subject_id in subjects
        ),
        group_order=(),
        condition_order=(),
        covariates=(),
        contrast=(1.0,),
        one_sample_baseline=0.0,
        mask_artifact_id="mask-1",
        tail=Tail.TWO_SIDED,
        missing_value_policy=MissingValuePolicy.ERROR,
        qc_review_revision_id="qc-1",
        qc_review_hash="a" * 64,
    )


def _correction() -> FdrCorrection:
    return FdrCorrection(
        method="fdr",
        q_threshold=0.05,
        mask_artifact_id="mask-1",
        statistic_type=StatisticalMapType.T,
        df1=2,
        df2=None,
    )


def _artifact(
    role: StatisticalArtifactRole,
    *,
    placeholder: bool = False,
    synthetic_type: bool = False,
) -> RegisteredArtifactMetadata:
    slug = role.value
    prefix = "synthetic.non_scientific" if synthetic_type else "statistics"
    return RegisteredArtifactMetadata(
        artifact_id=f"artifact-{slug}",
        role=role,
        artifact_type=f"{prefix}.{slug}",
        relative_path=f"results/{slug}.dat",
        checksum_sha256=None if placeholder else "b" * 64,
        size_bytes=None if placeholder else 128,
        provenance_hash=None if placeholder else "c" * 64,
        placeholder=placeholder,
    )


def _artifacts(
    *,
    corrected: bool = True,
    placeholder: bool = False,
    synthetic_types: bool = False,
) -> tuple[RegisteredArtifactMetadata, ...]:
    roles = [
        StatisticalArtifactRole.DESIGN_MATRIX,
        StatisticalArtifactRole.CONTRAST,
        StatisticalArtifactRole.UNCORRECTED_STATISTICAL_MAP,
        StatisticalArtifactRole.EFFECT_MAP,
        StatisticalArtifactRole.CLUSTER_TABLE,
        StatisticalArtifactRole.EXECUTION_LOG,
        StatisticalArtifactRole.SOFTWARE_VERSION_EVIDENCE,
    ]
    if corrected:
        roles.append(StatisticalArtifactRole.CORRECTED_STATISTICAL_MAP)
    return tuple(
        _artifact(role, placeholder=placeholder, synthetic_type=synthetic_types) for role in roles
    )


def _real_manifest() -> StatisticalResultManifest:
    return StatisticalResultManifest(
        result_id="result-1",
        run_id="run-1",
        design_revision_id="stats-result-1",
        mode=StatisticalResultMode.REAL,
        non_scientific=False,
        non_scientific_reason=None,
        correction=_correction(),
        cluster_connectivity_definition="explicit executor evidence: connectivity=METHOD-X",
        artifacts=_artifacts(),
        clusters=(
            ClusterRecord(
                cluster_id="cluster-1",
                source_map_artifact_id="artifact-corrected_statistical_map",
                extent_voxels=12,
                peak_statistic=4.25,
                peak_coordinate_mm=(2.0, -4.0, 6.0),
                coordinate_space="MNI",
            ),
        ),
    )


def test_real_report_is_complete_deterministic_and_method_neutral() -> None:
    manifest = _real_manifest()
    kwargs = {
        "manifest": manifest,
        "design": _design(),
        "correction": _correction(),
        "qc_review_hash": "a" * 64,
        "environment_hash": "d" * 64,
        "plan_hash": "e" * 64,
    }

    first = build_statistical_reproducibility_report(**kwargs)
    second = build_statistical_reproducibility_report(**kwargs)

    assert first == second
    assert len(first.bundle_hash) == 64
    payload = json.loads(first.json_text)
    assert payload["frozen_bindings"]["qc_review_hash"] == "a" * 64
    assert payload["statistical_design"]["subject_order"] == ["sub-01", "sub-02", "sub-03"]
    assert payload["design_matrix"] == [[1.0], [1.0], [1.0]]
    assert payload["contrast"] == [1.0]
    assert payload["correction"]["method"] == "fdr"
    assert payload["cluster_connectivity_definition"].endswith("METHOD-X")
    assert "Cohen" not in first.markdown
    assert "connectivity=METHOD-X" in first.markdown


@pytest.mark.parametrize(
    "missing_role",
    [
        StatisticalArtifactRole.DESIGN_MATRIX,
        StatisticalArtifactRole.CONTRAST,
        StatisticalArtifactRole.UNCORRECTED_STATISTICAL_MAP,
        StatisticalArtifactRole.CORRECTED_STATISTICAL_MAP,
        StatisticalArtifactRole.EFFECT_MAP,
        StatisticalArtifactRole.CLUSTER_TABLE,
        StatisticalArtifactRole.EXECUTION_LOG,
        StatisticalArtifactRole.SOFTWARE_VERSION_EVIDENCE,
    ],
)
def test_real_manifest_fails_closed_when_required_evidence_is_missing(
    missing_role: StatisticalArtifactRole,
) -> None:
    values = _real_manifest().model_dump(mode="python")
    values["artifacts"] = tuple(
        artifact for artifact in values["artifacts"] if artifact["role"] != missing_role
    )

    with pytest.raises(ValidationError, match="missing artifact roles"):
        StatisticalResultManifest.model_validate(values)


def test_real_manifest_rejects_placeholders() -> None:
    values = _real_manifest().model_dump(mode="python")
    values["artifacts"] = _artifacts(placeholder=True)

    with pytest.raises(ValidationError, match="forbids placeholder"):
        StatisticalResultManifest.model_validate(values)


def test_result_manifest_rejects_artifact_type_that_impersonates_another_role() -> None:
    values = _real_manifest().model_dump(mode="python")
    design_matrix = next(
        artifact
        for artifact in values["artifacts"]
        if artifact["role"] == StatisticalArtifactRole.DESIGN_MATRIX
    )
    design_matrix["artifact_type"] = "statistics.execution_log"

    with pytest.raises(ValidationError, match="role/type mismatch"):
        StatisticalResultManifest.model_validate(values)


def test_uncorrected_manifest_requires_no_corrected_map() -> None:
    values = _real_manifest().model_dump(mode="python")
    values["correction"] = None
    values["artifacts"] = _artifacts(corrected=False)
    values["clusters"] = ()

    manifest = StatisticalResultManifest.model_validate(values)
    assert manifest.correction is None
    assert not manifest.artifacts_for_role(StatisticalArtifactRole.CORRECTED_STATISTICAL_MAP)

    values["artifacts"] = _artifacts(corrected=True)
    with pytest.raises(ValidationError, match="requires an explicit CorrectionSpec"):
        StatisticalResultManifest.model_validate(values)


def test_synthetic_manifest_allows_placeholders_but_forces_visible_warning() -> None:
    manifest = StatisticalResultManifest(
        result_id="fixture-result",
        run_id="fixture-run",
        design_revision_id="stats-result-1",
        mode=StatisticalResultMode.SYNTHETIC_NON_SCIENTIFIC,
        non_scientific=True,
        non_scientific_reason="Fixture metadata exercises the reporting workflow only.",
        correction=_correction(),
        cluster_connectivity_definition="synthetic fixture declaration; no scientific method",
        artifacts=_artifacts(placeholder=True, synthetic_types=True),
        clusters=(),
    )

    report = build_statistical_reproducibility_report(
        manifest=manifest,
        design=_design(),
        correction=_correction(),
        qc_review_hash="a" * 64,
        environment_hash="d" * 64,
        plan_hash="e" * 64,
    )

    payload = json.loads(report.json_text)
    assert payload["result"]["non_scientific"] is True
    assert "must not be used for scientific or clinical inference" in payload["result"]["warning"]
    assert "SYNTHETIC / NON-SCIENTIFIC RESULT" in report.markdown
    assert "NOT AVAILABLE" in report.markdown


def test_synthetic_manifest_requires_non_scientific_reason() -> None:
    with pytest.raises(ValidationError, match="non-scientific marker and reason"):
        StatisticalResultManifest(
            result_id="fixture-result",
            run_id="fixture-run",
            design_revision_id="stats-result-1",
            mode=StatisticalResultMode.SYNTHETIC_NON_SCIENTIFIC,
            non_scientific=False,
            non_scientific_reason=None,
            correction=None,
            cluster_connectivity_definition="fixture-only declaration",
            artifacts=_artifacts(
                corrected=False,
                placeholder=True,
                synthetic_types=True,
            ),
            clusters=(),
        )


def test_report_rejects_changed_qc_or_correction_bindings() -> None:
    with pytest.raises(ValueError, match="QC hash"):
        build_statistical_reproducibility_report(
            manifest=_real_manifest(),
            design=_design(),
            correction=_correction(),
            qc_review_hash="f" * 64,
            environment_hash="d" * 64,
            plan_hash="e" * 64,
        )

    with pytest.raises(ValueError, match="CorrectionSpec"):
        build_statistical_reproducibility_report(
            manifest=_real_manifest(),
            design=_design(),
            correction=None,
            qc_review_hash="a" * 64,
            environment_hash="d" * 64,
            plan_hash="e" * 64,
        )


def test_cluster_record_requires_explicit_finite_observations_and_registered_map() -> None:
    with pytest.raises(ValidationError, match="peak statistic must be finite"):
        ClusterRecord(
            cluster_id="cluster-invalid",
            source_map_artifact_id="map",
            extent_voxels=1,
            peak_statistic=float("nan"),
            peak_coordinate_mm=(0.0, 0.0, 0.0),
            coordinate_space="MNI",
        )

    values = _real_manifest().model_dump(mode="python")
    values["clusters"][0]["source_map_artifact_id"] = "not-registered"
    with pytest.raises(ValidationError, match="registered statistical maps"):
        StatisticalResultManifest.model_validate(values)
