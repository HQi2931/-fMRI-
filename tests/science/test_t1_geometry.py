"""Small NIfTI headers verify SPM one-based geometry without imaging software."""

import struct

import pytest

from neuroagent.infrastructure.filesystem.t1_geometry import verify_t1_geometry


def test_actual_geometry_matches_spm_affine_and_rejects_forged_evidence(tmp_path):
    raw = bytearray(384)
    struct.pack_into("<i", raw, 0, 348)
    struct.pack_into("<8h", raw, 40, 3, 2, 2, 2, 1, 1, 1, 1)
    struct.pack_into("<h", raw, 70, 16)
    struct.pack_into("<h", raw, 72, 32)
    struct.pack_into("<8f", raw, 76, 1, 1, 1, 1, 0, 0, 0, 0)
    struct.pack_into("<f", raw, 108, 352)
    struct.pack_into("<h", raw, 254, 1)
    for offset, row in zip(
        (280, 296, 312), ((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0)), strict=True
    ):
        struct.pack_into("<4f", raw, offset, *row)
    raw[344:348] = b"n+1\0"
    path = tmp_path / "coT1.nii"
    path.write_bytes(raw)
    metadata = {
        "dimensions": [2, 2, 2],
        "affine": [[1, 0, 0, -1], [0, 1, 0, -1], [0, 0, 1, -1], [0, 0, 0, 1]],
    }
    verify_t1_geometry(path, metadata)
    with pytest.raises(ValueError, match="dimensions differ"):
        verify_t1_geometry(path, {**metadata, "dimensions": [3, 2, 2]})
    with pytest.raises(ValueError, match="affine differs"):
        verify_t1_geometry(
            path, {**metadata, "affine": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]}
        )
