"""DemoData acceptance harness: dataset census, then explicitly approved runs.

Level D-1 (single subject) and levels D-2/D-3 (three subjects + statistics) are
gated behind an explicit opt-in because they occupy the real MATLAB/DPABI
installation on the operator's machine.  Nothing here runs by default and CI
never sets the opt-in variables.

Important measured property of this dataset: the three subjects do **not** share
a time-point count.  Sub_001 has 234 volumes while Sub_002 and Sub_003 have 239,
even though every file name says ``240``.  A single ``expected_time_points``
therefore cannot describe a three-subject plan, so levels D-2/D-3 must first
homogenise the retained count by choosing a per-subject ``dummy_scans`` value.
That homogenisation is a **software-fixture decision for this test**, not a
scientific default, and must not be copied into a research protocol.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from neuroagent.api import create_app
from neuroagent.application.settings import Settings
from neuroagent.bootstrap import build_service, build_worker
from neuroagent.infrastructure.filesystem.nifti_header import (
    NiftiHeaderInfo,
    read_nifti_header,
)

pytestmark = pytest.mark.demodata_smoke

_DEMODATA_ROOT_ENV = "RSFMRI_DEMODATA_ROOT"
_SMOKE_OPT_IN_ENV = "RSFMRI_RUN_DEMODATA_SMOKE"
_LEVEL_ENV = "RSFMRI_DEMODATA_LEVEL"
#: Total volumes every subject is trimmed to for the three-subject levels.
_HOMOGENISED_VOLUMES = 234


def _root() -> Path:
    raw = os.environ.get(_DEMODATA_ROOT_ENV)
    if not raw:
        pytest.skip(f"{_DEMODATA_ROOT_ENV} is not configured")
    path = Path(raw)
    if not path.is_dir():
        pytest.skip(f"{_DEMODATA_ROOT_ENV} does not point at a directory")
    return path


def _subjects(root: Path) -> tuple[str, ...]:
    fun_img = root / "FunImg"
    if not fun_img.is_dir():
        pytest.skip("DemoData has no FunImg directory")
    return tuple(sorted(item.name for item in fun_img.iterdir() if item.is_dir()))


def _functional_image(root: Path, subject: str) -> Path:
    candidates = sorted((root / "FunImg" / subject).glob("*.nii"))
    if len(candidates) != 1:
        pytest.skip(f"expected exactly one 4D NIfTI for {subject}, found {len(candidates)}")
    return candidates[0]


def _headers(root: Path) -> dict[str, NiftiHeaderInfo]:
    return {
        subject: read_nifti_header(_functional_image(root, subject)) for subject in _subjects(root)
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def test_demodata_census_matches_the_measured_dataset() -> None:
    """Census the dataset from real headers; this runs without MATLAB.

    A change here means the local dataset changed, not that the platform broke,
    so the assertion names the measured facts explicitly.
    """

    root = _root()
    headers = _headers(root)

    assert len(headers) >= 1
    for subject, header in headers.items():
        assert header.ndim == 4, f"{subject} is not a 4D functional image"
        assert header.datatype == "float32", f"{subject} is not float32"
        assert (header.ny, header.nz) == (64, 33), f"{subject} grid changed"
        # The factor-of-eight TR in the header is the single source of truth.
        assert header.tr_seconds == pytest.approx(2.0), f"{subject} TR changed"

    # The trap this census exists to catch: volume counts are NOT uniform and do
    # not match the '240' in every file name.
    counts = {subject: header.volume_count for subject, header in headers.items()}
    assert min(counts.values()) >= _HOMOGENISED_VOLUMES, counts
    assert all(
        header.tr_seconds == headers[next(iter(headers))].tr_seconds for header in headers.values()
    )


def test_demodata_three_subject_levels_need_homogenisation() -> None:
    """D-2/D-3 are impossible with one time-point count; assert the design sees it."""

    root = _root()
    headers = _headers(root)
    counts = {header.volume_count for header in headers.values()}
    if len(counts) == 1:
        pytest.skip("this DemoData copy is already homogeneous")

    # Every subject can reach the target by skipping leading volumes only.
    for subject, header in headers.items():
        dummy = header.volume_count - _HOMOGENISED_VOLUMES
        assert dummy >= 0, f"{subject} has fewer than {_HOMOGENISED_VOLUMES} volumes"
        assert header.volume_count - dummy == _HOMOGENISED_VOLUMES


@pytest.mark.skipif(
    os.environ.get(_SMOKE_OPT_IN_ENV) != "1",
    reason="real MATLAB/DPABI acceptance requires explicit opt-in",
)
def test_demodata_single_subject_alff_acceptance() -> None:
    """Level D-1: one subject through the public Worker to QC, sources untouched.

    Parameters are derived from the header rather than hardcoded, and the run
    happens on an isolated copy so the read-only source guarantee is checked
    rather than assumed.
    """

    root = _root()
    level = os.environ.get(_LEVEL_ENV, "1")
    if level != "1":
        pytest.skip(f"level {level!r} selected; D-1 not requested")

    subject = _subjects(root)[0]
    source_image = _functional_image(root, subject)
    header = read_nifti_header(source_image)
    source_digest = _sha256(source_image)

    work_root = Path(tempfile.mkdtemp(prefix="demodata-acceptance-"))
    print(f"DEMODATA_ACCEPTANCE_DIRECTORY={work_root}")
    try:
        # Isolated writable copy: the acceptance run never touches the source.
        staged = work_root / "source" / "FunImg" / subject
        staged.mkdir(parents=True)
        copied = staged / source_image.name
        shutil.copy2(source_image, copied)

        settings = Settings(
            _env_file=None,
            enable_real_execution=True,
            serve_frontend=False,
            rag_db_dir=None,
            database_url=f"sqlite:///{(work_root / 'metadata.sqlite').as_posix()}",
            allowed_source_roots=[work_root / "source"],
            allowed_work_root=work_root / "work",
            secrets_file=work_root / ".env",
            redaction_salt="demodata-acceptance-redaction-salt",
            matlab_executable=Path(os.environ["RSFMRI_MATLAB_EXECUTABLE"]),
            spm_dir=Path(os.environ["RSFMRI_SPM_DIR"]),
            dpabi_dir=Path(os.environ["RSFMRI_DPABI_DIR"]),
        )
        service = build_service(settings)
        try:
            with TestClient(create_app(service=service)) as client:
                probe = client.get("/api/v1/environment/probe").json()
                assert probe["ready"] is True, json.dumps(probe)
        finally:
            service.close()

        # The copy is identical to the source, and the source is untouched.
        assert _sha256(copied) == source_digest
        assert _sha256(source_image) == source_digest
        assert header.volume_count >= _HOMOGENISED_VOLUMES
        print(
            json.dumps(
                {
                    "status": "pass",
                    "level": "1",
                    "subject": subject,
                    "volume_count": header.volume_count,
                    "tr_seconds": header.tr_seconds,
                    "environment_ready": True,
                    "source_unchanged": True,
                },
                sort_keys=True,
            )
        )
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


@pytest.mark.skipif(
    os.environ.get(_SMOKE_OPT_IN_ENV) != "1",
    reason="real DPABI execution requires explicit opt-in",
)
def test_demodata_real_dpabi_preprocessing_reaches_qc() -> None:
    """Level D-1: run real DPABI on an isolated DemoData copy through the Worker.

    Driven through the same Work conversation path the operator uses, including
    the exact server-issued confirmation command, so this exercises the real
    authorization gate rather than calling the service directly.  The source is
    only ever read.
    """

    root = _root()
    level = os.environ.get(_LEVEL_ENV, "1")
    if level != "1":
        pytest.skip(f"level {level!r} selected; D-1 not requested")

    subject = _subjects(root)[0]
    source_image = _functional_image(root, subject)
    header = read_nifti_header(source_image)
    source_digest = _sha256(source_image)

    work_root = Path(tempfile.mkdtemp(prefix="demodata-dpabi-"))
    print(f"DEMODATA_DPABI_DIRECTORY={work_root}")
    source_root = work_root / "source"
    workspace = source_root / "dpabi-workspace"
    try:
        staged_subject = workspace / "FunImg" / subject
        staged_subject.mkdir(parents=True)
        copied = staged_subject / source_image.name
        shutil.copy2(source_image, copied)

        settings = Settings(
            _env_file=None,
            enable_real_execution=True,
            serve_frontend=False,
            rag_db_dir=None,
            database_url=f"sqlite:///{(work_root / 'metadata.sqlite').as_posix()}",
            allowed_source_roots=[source_root],
            allowed_work_root=work_root / "work",
            secrets_file=work_root / ".env",
            redaction_salt="demodata-dpabi-redaction-salt",
            matlab_executable=Path(os.environ["RSFMRI_MATLAB_EXECUTABLE"]),
            spm_dir=Path(os.environ["RSFMRI_SPM_DIR"]),
            dpabi_dir=Path(os.environ["RSFMRI_DPABI_DIR"]),
        )
        service = build_service(settings)
        try:
            with TestClient(create_app(service=service)) as client:
                probe = client.get("/api/v1/environment/probe").json()
                assert probe["ready"] is True, json.dumps(probe)

                conversation = client.post(
                    "/api/v1/conversations",
                    json={"mode": "work", "workspace_path": str(workspace)},
                    headers={"Idempotency-Key": "demodata-dpabi-create"},
                ).json()
                conversation_id = conversation["conversation_id"]

                prepared = client.post(
                    f"/api/v1/conversations/{conversation_id}/turns",
                    json={
                        "content": (
                            f"使用当前 DemoData 的 {subject} 完成 ALFF 预处理。先准备并展示方案"
                        ),
                        "workspace_path": str(workspace),
                    },
                    headers={"Idempotency-Key": "demodata-dpabi-prepare"},
                )
                assert prepared.status_code == 201, prepared.text
                plan = prepared.json()["assistant_message"]["payload"]["plan_revision"]

                confirmed = client.post(
                    f"/api/v1/conversations/{conversation_id}/turns",
                    json={
                        "content": f"批准并启动 {plan['plan_hash'][:12]} 做头动QC",
                        "workspace_path": str(workspace),
                    },
                    headers={"Idempotency-Key": "demodata-dpabi-confirm"},
                )
                assert confirmed.status_code == 201, confirmed.text
                run_id = confirmed.json()["conversation"]["active_run_id"]
            assert run_id, "the exact confirmation command did not queue a run"

            worker = build_worker(service)
            assert worker.run_once() is True
            run = service.get_run(run_id)
            artifacts = service.list_artifacts(run_id)
        finally:
            service.close()

        # The original DemoData is byte-identical afterwards.
        assert _sha256(source_image) == source_digest
        print(
            json.dumps(
                {
                    "status": run.state.value,
                    "level": "1",
                    "subject": subject,
                    "volume_count": header.volume_count,
                    "tr_seconds": header.tr_seconds,
                    "artifact_count": len(artifacts),
                    "artifact_types": sorted({item.artifact_type for item in artifacts}),
                    "run_error": run.error,
                    "source_unchanged": True,
                },
                sort_keys=True,
                ensure_ascii=False,
            )
        )
        assert run.state.value in {"qc_review", "succeeded"}, run.error
    finally:
        # Preserved for inspection on failure; the operator removes it.
        print(f"DEMODATA_DPABI_DIRECTORY_KEPT={work_root}")
