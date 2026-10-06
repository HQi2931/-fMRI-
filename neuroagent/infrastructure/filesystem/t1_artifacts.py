"""Validate final prepared T1 and optional inspection image artifacts."""

import gzip
import math
import struct
from pathlib import Path

from neuroagent.infrastructure.filesystem.nifti_header import read_nifti_header


def validate_t1_artifact(path: Path, role: str) -> None:
    if role == "preprocessing.t1_prepared":
        header = read_nifti_header(path)
        if header.ndim != 3 or not all(math.isfinite(v) and v > 0 for v in header.pixdim_mm):
            raise ValueError("prepared T1 requires finite 3D geometry")
        bytes_per_voxel = {
            "uint8": 1,
            "int8": 1,
            "int16": 2,
            "uint16": 2,
            "float32": 4,
            "int32": 4,
            "uint32": 4,
            "float64": 8,
        }[header.datatype]
        minimum_size = 352 + header.voxel_count * bytes_per_voxel
        opener = gzip.open if path.name.lower().endswith(".gz") else open
        with opener(path, "rb") as stream:
            stream.seek(minimum_size - 1)
            if len(stream.read(1)) != 1:
                raise ValueError("prepared T1 voxel payload is incomplete")
    elif role == "preprocessing.t1_inspection_image":
        with path.open("rb") as stream:
            header_bytes = stream.read(33)
        if (
            len(header_bytes) != 33
            or header_bytes[:8] != b"\x89PNG\r\n\x1a\n"
            or header_bytes[12:16] != b"IHDR"
        ):
            raise ValueError("T1 inspection image must be PNG")
        width, height = struct.unpack(">II", header_bytes[16:24])
        if not (1 <= width <= 16384 and 1 <= height <= 16384):
            raise ValueError("T1 inspection image geometry is invalid")
