from __future__ import annotations

import gzip
import hashlib
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from neuroagent.api import create_app
from neuroagent.application.artifact_content import nifti_preview_png
from neuroagent.application.contracts import RunCreate
from neuroagent.application.errors import InputValidationError
from neuroagent.application.services import NeuroAgentService

from .conftest import make_approved_plan, make_project


def _nifti() -> bytes:
    header = bytearray(352)
    struct.pack_into("<i", header, 0, 348)
    struct.pack_into("<8h", header, 40, 3, 2, 3, 2, 1, 1, 1, 1)
    struct.pack_into("<2h", header, 70, 16, 32)
    struct.pack_into("<f", header, 108, 352)
    header[344:348] = b"n+1\0"
    return bytes(header) + struct.pack("<12f", *range(12))


def test_registered_download_and_png_preview_enforce_integrity(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
        ),
        "content-run",
    )
    path = Path(project.work_root) / run.run_id / "output" / "result.nii"
    path.parent.mkdir(parents=True)
    payload = _nifti()
    path.write_bytes(payload)
    service.repository.register_artifacts(
        project.project_id,
        run.run_id,
        [
            {
                "artifact_type": "metric.alff",
                "relative_path": "output/result.nii",
                "checksum": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
                "provenance": {"non_scientific": True},
            }
        ],
    )
    artifact = service.list_artifacts(run.run_id)[0]
    with TestClient(create_app(service=service)) as client:
        response = client.get(f"/api/v1/artifacts/{artifact.artifact_id}/content")
        assert response.status_code == 200
        assert response.content == payload
        assert "attachment" in response.headers["content-disposition"]
        preview = client.get(f"/api/v1/artifacts/{artifact.artifact_id}/preview.png")
        assert preview.status_code == 200
        assert preview.headers["content-type"] == "image/png"
        assert preview.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert struct.unpack(">II", preview.content[16:24]) == (2, 3)
        assert (
            preview.content
            == client.get(f"/api/v1/artifacts/{artifact.artifact_id}/preview.png").content
        )
        # Same-size mutation must fail both endpoints, without exposing local paths.
        path.write_bytes(payload[:-4] + b"\0\0\0\0")
        for suffix in ("content", "preview.png"):
            rejected = client.get(f"/api/v1/artifacts/{artifact.artifact_id}/{suffix}")
            assert rejected.status_code == 422
            assert rejected.json()["error"]["code"] == "artifact_integrity_changed"
            assert str(work_root) not in rejected.text
        assert client.get("/api/v1/artifacts/missing/content").status_code == 404


def test_preview_compressed_and_invalid_data_are_bounded(tmp_path: Path) -> None:
    plain = tmp_path / "result.nii"
    compressed = tmp_path / "result.nii.gz"
    plain.write_bytes(_nifti())
    compressed.write_bytes(gzip.compress(_nifti()))
    assert nifti_preview_png(plain) == nifti_preview_png(compressed)
    header = bytearray(_nifti())
    struct.pack_into("<h", header, 42, 32767)
    plain.write_bytes(header)
    with pytest.raises(InputValidationError, match="无法生成"):
        nifti_preview_png(plain)
    compressed.write_bytes(b"broken")
    with pytest.raises(InputValidationError, match="无法生成"):
        nifti_preview_png(compressed)


@pytest.mark.parametrize("relative", ["../result.nii", "C:/result.nii", "output/../result.nii"])
def test_content_rejects_paths_even_if_registration_was_corrupted(
    tmp_path: Path, relative: str
) -> None:
    from types import SimpleNamespace

    from neuroagent.application.artifact_content import artifact_content_path

    artifact = SimpleNamespace(run_id="run", project_id="project", relative_path=relative)
    run = SimpleNamespace(run_id="run", project_id="project")
    project = SimpleNamespace(project_id="project", work_root=str(tmp_path))
    with pytest.raises(InputValidationError, match="路径无效"):
        artifact_content_path(artifact, run, project)


def test_content_rejects_links_and_missing_registered_file(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from neuroagent.application.artifact_content import artifact_content_path

    root = tmp_path / "run"
    root.mkdir()
    payload = b"registered-result"
    artifact = SimpleNamespace(
        run_id="run",
        project_id="project",
        relative_path="result.txt",
        size_bytes=len(payload),
        checksum=hashlib.sha256(payload).hexdigest(),
    )
    run = SimpleNamespace(run_id="run", project_id="project")
    project = SimpleNamespace(project_id="project", work_root=str(tmp_path))
    with pytest.raises(InputValidationError, match="不可用"):
        artifact_content_path(artifact, run, project)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(payload)
    try:
        (root / "result.txt").symlink_to(outside)
    except OSError:
        pytest.skip("This Windows host cannot create symbolic links")
    with pytest.raises(InputValidationError, match="包含链接"):
        artifact_content_path(artifact, run, project)


def test_content_scope_is_checked_before_accessing_files(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from neuroagent.application.artifact_content import artifact_content_path

    artifact = SimpleNamespace(run_id="different-run", project_id="project")
    run = SimpleNamespace(run_id="run", project_id="project")
    project = SimpleNamespace(project_id="project", work_root=str(tmp_path))
    with pytest.raises(InputValidationError, match="范围不匹配"):
        artifact_content_path(artifact, run, project)
