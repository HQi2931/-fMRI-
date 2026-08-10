from __future__ import annotations

import pytest
from pydantic import ValidationError

from neuroagent.domain.fmri import (
    AlffFalffParameters,
    ArtifactLineage,
    FrequencyBand,
    MetricKind,
    MetricScaling,
    RehoParameters,
    TemporalFilterTiming,
    validate_alff_falff_input,
    validate_reho_input,
)
from tests.science.conftest import ALFF_PROVENANCE, REHO_PROVENANCE, provenance


def make_alff() -> AlffFalffParameters:
    return AlffFalffParameters(
        tr_seconds=2.0,
        frequency_band=FrequencyBand(low_hz=0.01, high_hz=0.08),
        requested_metrics=(MetricKind.ALFF, MetricKind.FALFF),
        requested_scalings=(MetricScaling.RAW, MetricScaling.Z_SCORE),
        mask_artifact_id="mask-001",
        filter_timing=TemporalFilterTiming.AFTER_NORMALIZE,
        result_smoothing=False,
        result_smoothing_fwhm_mm=None,
        provenance=provenance(ALFF_PROVENANCE),
    )


def make_reho() -> RehoParameters:
    return RehoParameters(
        tr_seconds=2.0,
        temporal_filter_band=FrequencyBand(low_hz=0.01, high_hz=0.08),
        temporal_filter_add_mean_back=True,
        cluster_voxels=27,
        mask_artifact_id="mask-001",
        requested_scalings=(MetricScaling.RAW, MetricScaling.Z_SCORE),
        smooth_reho=False,
        smooth_reho_fwhm_mm=None,
        global_result_smoothing=True,
        global_result_smoothing_fwhm_mm=(6.0, 6.0, 6.0),
        provenance=provenance(REHO_PROVENANCE),
    )


def test_valid_metric_contracts_accept_unsmoothed_input(base_lineage: ArtifactLineage) -> None:
    assert validate_alff_falff_input(base_lineage, make_alff()) == ()
    assert validate_reho_input(base_lineage, make_reho()) == ()


def test_falff_rejects_prefiltered_input(base_lineage: ArtifactLineage) -> None:
    filtered = base_lineage.model_copy(
        update={
            "temporally_filtered": True,
            "frequency_band": FrequencyBand(low_hz=0.01, high_hz=0.08),
        }
    )
    assert "FALFF_INPUT_ALREADY_FILTERED" in validate_alff_falff_input(filtered, make_alff())


def test_reho_rejects_spatially_smoothed_input(base_lineage: ArtifactLineage) -> None:
    smoothed = base_lineage.model_copy(
        update={"spatially_smoothed": True, "smoothing_fwhm_mm": (6.0, 6.0, 6.0)}
    )
    assert "REHO_INPUT_SPATIALLY_SMOOTHED" in validate_reho_input(smoothed, make_reho())


def test_reho_rejects_prefiltered_input_even_when_band_matches(
    base_lineage: ArtifactLineage,
) -> None:
    filtered = base_lineage.model_copy(
        update={
            "temporally_filtered": True,
            "frequency_band": make_reho().temporal_filter_band,
        }
    )

    assert validate_reho_input(filtered, make_reho()) == ("REHO_INPUT_ALREADY_FILTERED",)


@pytest.mark.parametrize("cluster", [0, 6, 8, 26, 28])
def test_reho_neighborhood_is_limited_to_dpabi_values(cluster: int) -> None:
    values = make_reho().model_dump()
    values["cluster_voxels"] = cluster
    with pytest.raises(ValidationError, match="7, 19 or 27"):
        RehoParameters.model_validate(values)


def test_reho_duplicate_smoothing_is_blocked() -> None:
    values = make_reho().model_dump()
    values.update(
        smooth_reho=True,
        smooth_reho_fwhm_mm=(4.0, 4.0, 4.0),
        global_result_smoothing=True,
        global_result_smoothing_fwhm_mm=(6.0, 6.0, 6.0),
    )
    with pytest.raises(ValidationError, match="twice"):
        RehoParameters.model_validate(values)


def test_frequency_band_must_not_exceed_nyquist() -> None:
    values = make_alff().model_dump()
    values["frequency_band"] = {"low_hz": 0.01, "high_hz": 0.3}
    with pytest.raises(ValidationError, match="Nyquist"):
        AlffFalffParameters.model_validate(values)


def test_scaled_products_require_mask() -> None:
    values = make_alff().model_dump()
    values["mask_artifact_id"] = None
    with pytest.raises(ValidationError, match="require a mask"):
        AlffFalffParameters.model_validate(values)


def test_metric_input_binds_tr_and_effective_frequency_resolution(
    base_lineage: ArtifactLineage,
) -> None:
    wrong_tr = make_alff().model_copy(update={"tr_seconds": 3.0})
    assert "TR_LINEAGE_MISMATCH" in validate_alff_falff_input(base_lineage, wrong_tr)

    below_resolution = make_alff().model_copy(
        update={"frequency_band": FrequencyBand(low_hz=0.001, high_hz=0.08)}
    )
    assert "FREQUENCY_BAND_BELOW_EFFECTIVE_RESOLUTION" in validate_alff_falff_input(
        base_lineage, below_resolution
    )
