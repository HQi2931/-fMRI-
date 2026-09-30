"""Compare actual NIfTI-1 geometry with SPM's one-based voxel affine."""

from __future__ import annotations

import math
import struct
from pathlib import Path
from typing import Any

from neuroagent.infrastructure.filesystem.nifti_header import read_nifti_header


def verify_t1_geometry(path: Path, metadata: dict[str, Any]) -> None:
    header = read_nifti_header(path)
    if metadata.get("dimensions") != [header.nx, header.ny, header.nz]:
        raise ValueError("T1 header dimensions differ from execution evidence")
    with path.open("rb") as stream:
        raw = stream.read(348)
    endian = "<" if header.little_endian else ">"
    qform, sform = struct.unpack_from(f"{endian}2h", raw, 252)
    if sform > 0:
        affine = [
            list(struct.unpack_from(f"{endian}4f", raw, offset)) for offset in (280, 296, 312)
        ]
    elif qform > 0:
        b, c, d = struct.unpack_from(f"{endian}3f", raw, 256)
        squared = b * b + c * c + d * d
        if squared > 1.0:
            scale = math.sqrt(squared)
            b, c, d = b / scale, c / scale, d / scale
            a = 0.0
        else:
            a = math.sqrt(1.0 - squared)
        rotation = [
            [a * a + b * b - c * c - d * d, 2 * b * c - 2 * a * d, 2 * b * d + 2 * a * c],
            [2 * b * c + 2 * a * d, a * a + c * c - b * b - d * d, 2 * c * d - 2 * a * b],
            [2 * b * d - 2 * a * c, 2 * c * d + 2 * a * b, a * a + d * d - c * c - b * b],
        ]
        qfac = struct.unpack_from(f"{endian}f", raw, 76)[0]
        spacing = (*header.pixdim_mm[:2], header.pixdim_mm[2] * (-1 if qfac < 0 else 1))
        offsets = struct.unpack_from(f"{endian}3f", raw, 268)
        affine = [
            [rotation[row][column] * spacing[column] for column in range(3)] + [offsets[row]]
            for row in range(3)
        ]
    else:
        raise ValueError("prepared T1 requires a declared qform or sform")
    # NIfTI coordinates start at zero; SPM spm_vol coordinates start at one.
    for row in affine:
        row[3] -= sum(row[:3])
    affine.append([0.0, 0.0, 0.0, 1.0])
    reported = metadata.get("affine")
    if not isinstance(reported, list) or len(reported) != 4:
        raise ValueError("T1 execution affine is missing")
    for actual_row, expected_row in zip(affine, reported, strict=True):
        if not isinstance(expected_row, list) or len(expected_row) != 4:
            raise ValueError("T1 execution affine is malformed")
        if any(
            not math.isfinite(float(value))
            or not math.isclose(float(value), actual, abs_tol=1e-4, rel_tol=1e-5)
            for actual, value in zip(actual_row, expected_row, strict=True)
        ):
            raise ValueError("T1 actual affine differs from execution evidence")
