from __future__ import annotations

import pytest

from neuroagent.application.errors import InputValidationError
from neuroagent.infrastructure.persistence.repository import (
    _validate_artifact_relative_path,
)


@pytest.mark.parametrize(
    "relative_path",
    (
        r"..\outside.nii.gz",
        r"C:\runs\artifact.nii.gz",
        r"\\server\share\artifact.nii.gz",
        "C:/runs/artifact.nii.gz",
        "//server/share/artifact.nii.gz",
        "/absolute/artifact.nii.gz",
        "../outside.nii.gz",
        "nested/../outside.nii.gz",
        "",
        ".",
    ),
)
def test_artifact_relative_path_rejects_unsafe_paths(relative_path: str) -> None:
    with pytest.raises(InputValidationError) as exc_info:
        _validate_artifact_relative_path(relative_path)

    assert exc_info.value.code == "artifact_path_invalid"
    assert exc_info.value.details == {"relative_path": relative_path}


@pytest.mark.parametrize(
    "relative_path",
    (
        "artifact.nii.gz",
        "nested/path/artifact.nii.gz",
        "output/sub-01/session-1/result.json",
    ),
)
def test_artifact_relative_path_accepts_canonical_posix_paths(relative_path: str) -> None:
    assert _validate_artifact_relative_path(relative_path) == relative_path


@pytest.mark.parametrize(
    "relative_path",
    (
        "./artifact.nii.gz",
        "nested//artifact.nii.gz",
        "nested/./artifact.nii.gz",
        "nested/artifact.nii.gz/",
    ),
)
def test_artifact_relative_path_rejects_noncanonical_posix_paths(relative_path: str) -> None:
    with pytest.raises(InputValidationError, match="规范相对路径"):
        _validate_artifact_relative_path(relative_path)
