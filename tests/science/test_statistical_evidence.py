from __future__ import annotations

import math

import pytest

from neuroagent.domain.fmri.statistical_evidence import (
    cohen_d_from_t,
    extract_26_connected_clusters,
    hedges_g_from_t,
)


def test_effect_size_definitions_are_fixed_and_finite() -> None:
    assert cohen_d_from_t(4.0, 16) == 1.0
    assert hedges_g_from_t(3.0, 10, 12, 20) == pytest.approx(
        (1 - 3 / 79) * 3 * math.sqrt(1 / 10 + 1 / 12)
    )
    with pytest.raises(ValueError):
        cohen_d_from_t(float("nan"), 4)


def test_clusters_use_26_connectivity_and_affine_mm_coordinates() -> None:
    clusters = extract_26_connected_clusters(
        {
            (0, 0, 0): 2.0,
            (1, 1, 1): 3.0,
            (4, 4, 4): 4.0,
            (4, 4, 5): 5.0,
        },
        threshold=1.0,
        affine=(
            (2.0, 0.0, 0.0, 10.0),
            (0.0, 3.0, 0.0, 20.0),
            (0.0, 0.0, 4.0, 30.0),
            (0.0, 0.0, 0.0, 1.0),
        ),
    )
    assert [(item.extent_voxels, item.peak_coordinate_mm) for item in clusters] == [
        (2, (12.0, 23.0, 34.0)),
        (2, (18.0, 32.0, 50.0)),
    ]
    assert (
        extract_26_connected_clusters(
            {},
            threshold=1,
            affine=tuple(
                tuple(float(v) for v in row)
                for row in ((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1))
            ),
        )
        == ()
    )
