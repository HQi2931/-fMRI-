from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from neuroagent.execution import (
    ArtifactPathBinding,
    ControlledMatlabExecutor,
    ExpectedArtifact,
    MatlabEnvironment,
    MatlabExecutionNotAuthorized,
    MatlabJobKind,
    MatlabJobSpec,
    MatlabTemplateRenderer,
    PreprocessingJobPayload,
)
from neuroagent.tools import DpabiCfgProjection


def job() -> MatlabJobSpec:
    return MatlabJobSpec(
        job_id="job-001",
        run_id="run-001",
        kind=MatlabJobKind.DPARSFA_PREPROCESSING,
        plan_hash="p" * 64,
        approval_record_id="approval-001",
        input_manifest_hash="m" * 64,
        timeout_seconds=3600,
        artifact_bindings=(
            ArtifactPathBinding(
                artifact_id="base-cfg", relative_path="input/base_cfg.mat", read_only=True
            ),
        ),
        expected_artifacts=(
            ExpectedArtifact(
                artifact_type="metric.alff",
                relative_pattern="output/Results/ALFF_*/ALFFMap_*.nii",
                required=True,
            ),
        ),
        payload=PreprocessingJobPayload(
            base_cfg_artifact_id="base-cfg",
            staging_relative_path="staging",
            metric_projection=DpabiCfgProjection(
                adapter_version="1.0.0",
                source_evidence=("DPARSFA_run.m:3925",),
                cfg={"IsCalALFF": 1, "TR": 2.0},
                cfg_hash="c" * 64,
            ),
            subject_ids=("sub-01",),
            base_cfg_allowed_fields=(),
        ),
    )


def environment(tmp_path: Path) -> MatlabEnvironment:
    return MatlabEnvironment(
        matlab_executable=tmp_path / "Program Files" / "MATLAB" / "bin" / "matlab.exe",
        matlab_root=tmp_path / "Program Files" / "MATLAB",
        spm_path=tmp_path / "Program Files" / "MATLAB" / "toolbox" / "spm12",
        dpabi_path=tmp_path / "Program Files" / "MATLAB" / "toolbox" / "DPABI_V8.2_240510",
        matlab_version="R2023b",
        spm_version="SPM12",
        dpabi_version="V8.2_240510",
    )


def test_renderer_uses_fixed_template_and_quotes_space_paths(tmp_path: Path) -> None:
    work = tmp_path / "work root"
    base = work / "run-001" / "input" / "base_cfg.mat"
    base.parent.mkdir(parents=True)
    base.write_bytes(b"synthetic-placeholder")
    renderer = MatlabTemplateRenderer(Path("matlab/templates"))
    rendered = renderer.render(job(), environment(tmp_path), work)
    script = rendered.entry_script.read_text(encoding="utf-8")
    assert "DPARSFA_run(cfg_mat_path, staging_directory, subject_list_path, 0)" in script
    assert "jsondecode" in script
    assert rendered.command[0].endswith("matlab.exe")
    assert "work root" in rendered.command[2]
    assert rendered.generated_files == (
        "scripts/bootstrap.m",
        "config/preprocessing.json",
        "subject_list.txt",
        "scripts/run_preprocessing.m",
        "provenance.json",
    )


def test_real_execution_is_disabled_by_default(tmp_path: Path) -> None:
    work = tmp_path / "work"
    base = work / "run-001" / "input" / "base_cfg.mat"
    base.parent.mkdir(parents=True)
    base.write_bytes(b"synthetic-placeholder")
    executor = ControlledMatlabExecutor(
        MatlabTemplateRenderer(Path("matlab/templates")), environment(tmp_path), work
    )
    assert executor.dry_run(job()).status.value == "dry_run"
    with pytest.raises(MatlabExecutionNotAuthorized):
        executor.execute(job(), is_cancelled=lambda: False)


def test_artifact_path_traversal_is_rejected() -> None:
    with pytest.raises(ValidationError, match="inside the run directory"):
        ArtifactPathBinding(artifact_id="bad", relative_path="../../raw/sub-01.nii", read_only=True)


def test_job_rejects_writable_sources_and_unapproved_base_cfg_fields() -> None:
    writable = job().model_dump(mode="python")
    writable["artifact_bindings"][0]["read_only"] = False
    with pytest.raises(ValidationError, match="read-only"):
        MatlabJobSpec.model_validate(writable)

    payload = job().payload.model_dump(mode="python")
    payload["base_cfg_allowed_fields"] = ("Covremove",)
    with pytest.raises(ValidationError, match="unsupported base Cfg"):
        PreprocessingJobPayload.model_validate(payload)
