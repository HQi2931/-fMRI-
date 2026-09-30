"""Revalidate actual prepared anatomy and inspection evidence at approval time."""

import gzip
import json
import math
import struct
from pathlib import Path

from neuroagent.infrastructure.filesystem.nifti_header import read_nifti_header


def validate_t1_preparation_artifact(path: Path, role: str) -> None:
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
    elif role == "preprocessing.t1_review_image":
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
    elif role == "preprocessing.metadata_evidence":
        evidence = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(evidence, dict)
            or evidence.get("operation") != "t1_preparation"
            or evidence.get("requires_human_review") is not True
        ):
            raise ValueError("T1 preparation evidence is invalid")
        outputs = evidence.get("outputs")
        if isinstance(outputs, dict):
            outputs = [outputs]
        if not isinstance(outputs, list) or not outputs:
            raise ValueError("T1 preparation outputs are missing")
        for output in outputs:
            if not isinstance(output, dict) or not isinstance(output.get("metadata"), dict):
                raise ValueError("T1 preparation header evidence is missing")
            metadata = output["metadata"]
            dimensions = metadata.get("dimensions")
            affine = metadata.get("affine")
            if (
                metadata.get("volume_count") != 1
                or not isinstance(dimensions, list)
                or len(dimensions) != 3
            ):
                raise ValueError("T1 preparation evidence must describe one 3D image")
            if not all(
                isinstance(v, (float, int)) and math.isfinite(v) and v >= 1 for v in dimensions
            ):
                raise ValueError("T1 preparation evidence dimensions are invalid")
            if (
                not isinstance(affine, list)
                or len(affine) != 4
                or not all(isinstance(row, list) and len(row) == 4 for row in affine)
            ):
                raise ValueError("T1 preparation evidence affine is invalid")
            if not all(
                isinstance(v, (float, int)) and math.isfinite(v) for row in affine for v in row
            ):
                raise ValueError("T1 preparation evidence affine is not finite")
            determinant = (
                affine[0][0] * (affine[1][1] * affine[2][2] - affine[1][2] * affine[2][1])
                - affine[0][1] * (affine[1][0] * affine[2][2] - affine[1][2] * affine[2][0])
                + affine[0][2] * (affine[1][0] * affine[2][1] - affine[1][1] * affine[2][0])
            )
            if abs(determinant) < 1e-12:
                raise ValueError("T1 preparation evidence affine is singular")
            root = path.parent.parent if path.parent.name == "output" else path.parent
            prepared_relative = output.get("relative_path")
            preview_relative = output.get("review_image")
            if not isinstance(prepared_relative, str) or not isinstance(preview_relative, str):
                raise ValueError("T1 preparation evidence has no bound image paths")
            prepared_path = (root / prepared_relative).resolve()
            preview_path = (root / preview_relative).resolve()
            if not prepared_path.is_relative_to(root.resolve()) or not preview_path.is_relative_to(
                root.resolve()
            ):
                raise ValueError("T1 preparation evidence image path escapes attempt")
            actual = read_nifti_header(prepared_path)
            # y_ReadRPI can permute axes; the complete voxel dimensions remain identical.
            if actual.ndim != 3 or sorted(dimensions) != sorted([actual.nx, actual.ny, actual.nz]):
                raise ValueError("T1 preparation evidence differs from actual image header")
            validate_t1_preparation_artifact(preview_path, "preprocessing.t1_review_image")
