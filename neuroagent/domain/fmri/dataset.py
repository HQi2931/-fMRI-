"""Stable dataset classification used across inspection and execution."""

from __future__ import annotations

from enum import StrEnum


class DatasetKind(StrEnum):
    BIDS = "bids"
    DPABI_READY = "dpabi_ready"
    DICOM = "dicom"
    NIFTI = "nifti"
    MIXED = "mixed"
    UNKNOWN = "unknown"
