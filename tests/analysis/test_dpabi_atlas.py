from __future__ import annotations

import os

import nibabel as nib
import numpy as np
import pytest
from scipy.io import savemat

from neuroagent.analysis.dpabi_atlas import _load_nifti, _read_labels, peak_axial_slice


def test_peak_axial_slice_uses_the_statistical_map_affine(tmp_path) -> None:
    affine = np.array(
        [[2.0, 0.0, 0.0, -10.0], [0.0, 2.0, 0.0, -20.0], [0.0, 0.0, 3.0, -30.0], [0, 0, 0, 1]],
        dtype=float,
    )
    path = tmp_path / "statistic.nii"
    nib.save(nib.Nifti1Image(np.ones((5, 5, 6), dtype=np.float32), affine), path)
    world = tuple((affine @ np.array([2, 1, 4, 1], dtype=float))[:3])

    assert peak_axial_slice(map_path=path, coordinate_mm=world) == 4

    with pytest.raises(ValueError, match="超出统计来源图网格"):
        peak_axial_slice(map_path=path, coordinate_mm=(0.0, 0.0, 100.0))


def test_nifti_cache_refreshes_when_content_changes_without_size_or_timestamp_change(tmp_path):
    path = tmp_path / "atlas.nii"
    data = np.ones((3, 4, 5), dtype=np.int16)
    nib.save(nib.Nifti1Image(data, np.eye(4)), path)
    previous = _load_nifti(path)
    metadata = path.stat()
    changed = np.eye(4)
    changed[0, 3] = 10
    nib.save(nib.Nifti1Image(data, changed), path)
    assert path.stat().st_size == metadata.st_size
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    refreshed = _load_nifti(path)
    assert refreshed is not previous and refreshed.affine[0, 3] == 10
    assert _load_nifti(path) is refreshed


def test_label_cache_refreshes_when_content_changes_without_size_or_timestamp_change(tmp_path):
    path = tmp_path / "labels.mat"
    savemat(path, {"Reference": np.array([["Old region", 1], ["Second", 2]], dtype=object)})
    previous = _read_labels(path)
    assert previous[1] == "Old region"
    metadata = path.stat()
    savemat(path, {"Reference": np.array([["New region", 1], ["Second", 2]], dtype=object)})
    assert path.stat().st_size == metadata.st_size
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    refreshed = _read_labels(path)
    assert refreshed is not previous and refreshed[1] == "New region"
    assert _read_labels(path) is refreshed
