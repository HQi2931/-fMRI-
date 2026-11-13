"""Descriptive subject means within a verified significant cluster."""

from pathlib import Path
from typing import cast

import nibabel as nib
import numpy as np
from nibabel.spatialimages import SpatialImage

from neuroagent.analysis.dpabi_atlas import cluster_voxel_mask
from neuroagent.domain.fmri.results import ClusterRecord


def extract_cluster_means(
    *, map_path: Path, cluster: ClusterRecord, image_paths: tuple[Path, ...]
) -> tuple[float, ...]:
    mask = cluster_voxel_mask(map_path, cluster)
    stored_reference = cast(SpatialImage, nib.load(str(map_path)))
    # DPABI may flip storage axes while preserving the world-coordinate grid.
    # Canonicalization only permutes/flips axes; it never resamples voxel values.
    # These nibabel constructors/helpers do not ship typed function signatures.
    reference = cast(SpatialImage, nib.as_closest_canonical(stored_reference))  # type: ignore[no-untyped-call]
    mask_image = nib.as_closest_canonical(  # type: ignore[no-untyped-call]
        nib.Nifti1Image(mask.astype(np.uint8), stored_reference.affine)  # type: ignore[no-untyped-call]
    )
    mask = np.asarray(mask_image.dataobj, dtype=bool)
    output: list[float] = []
    for path in image_paths:
        image = cast(SpatialImage, nib.as_closest_canonical(nib.load(str(path))))  # type: ignore[no-untyped-call]
        if image.shape != mask.shape or not np.allclose(
            image.affine, reference.affine, rtol=0, atol=1e-5
        ):
            raise ValueError("ROI metric image and statistical map have different grids")
        values = np.asarray(image.dataobj, dtype=np.float64)[mask]
        if not np.isfinite(values).all():
            raise ValueError("ROI contains invalid metric values")
        output.append(float(np.mean(values)))
    return tuple(output)
