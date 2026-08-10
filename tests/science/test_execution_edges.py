from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import neuroagent.execution.matlab as matlab_module
from neuroagent.execution import (
    ArtifactPathBinding,
    ControlledMatlabExecutor,
    ExpectedArtifact,
    MatlabEnvironment,
    MatlabExecutionError,
    MatlabJobKind,
    MatlabJobSpec,
    MatlabTemplateRenderer,
    MockMatlabExecutor,
    StatisticsJobPayload,
)
from neuroagent.execution.matlab import _assert_within, _replace_tokens, _write_stable
from neuroagent.tools.dpabi_v82 import (
    CorrectionCall,
    CorrectionFunction,
    StatisticsCall,
    StatisticsFunction,
)
from tests.science.test_matlab_execution import environment, job


def statistics_job(*, correction: CorrectionCall | None = None) -> MatlabJobSpec:
    call = StatisticsCall(
        function=StatisticsFunction.TTEST2,
        ordered_subject_ids=("sub-01", "sub-02"),
        dependent_artifact_groups=(("image-1",), ("image-2",)),
        other_covariates_by_group=(((20.0,),), ((30.0,),)),
        mask_artifact_id="mask",
        design_matrix=((1.0, 1.0, 20.0), (-1.0, 1.0, 30.0)),
        contrast=(1.0, 0.0, 0.0),
        one_sample_baseline=None,
        tail="two_sided",
        residual_df=1,
        revision_id="stats-1",
        design_hash="d" * 64,
    )
    return MatlabJobSpec(
        job_id="stats-job",
        run_id="stats-run",
        kind=MatlabJobKind.DPABI_STATISTICS,
        plan_hash="p" * 64,
        approval_record_id="approval-1",
        input_manifest_hash="m" * 64,
        timeout_seconds=30,
        artifact_bindings=(
            ArtifactPathBinding(artifact_id="image-1", relative_path="input/a.nii", read_only=True),
            ArtifactPathBinding(artifact_id="image-2", relative_path="input/b.nii", read_only=True),
            ArtifactPathBinding(artifact_id="mask", relative_path="input/mask.nii", read_only=True),
        ),
        expected_artifacts=(
            ExpectedArtifact(
                artifact_type="statistics.map",
                relative_pattern="output/statistic.nii",
                required=True,
            ),
        ),
        payload=StatisticsJobPayload(statistics=call, correction=correction),
    )


def fdr_correction() -> CorrectionCall:
    return CorrectionCall(
        function=CorrectionFunction.FDR,
        parameters={
            "q_threshold": 0.05,
            "mask_artifact_id": "mask",
            "statistic_type": "T",
            "df1": 1.0,
            "df2": None,
        },
        correction_hash="f" * 64,
    )


def test_statistics_renderer_materializes_registered_paths_and_correction(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    rendered = MatlabTemplateRenderer(Path("matlab/templates")).render(
        statistics_job(correction=fdr_correction()), environment(tmp_path), work
    )
    payload = json.loads(
        (rendered.run_directory / "config" / "statistics.json").read_text(encoding="utf-8")
    )
    assert payload["function"] == "y_TTest2_Image"
    assert payload["dependent_groups"] == [
        {"paths": [{"value": str(rendered.run_directory / "input" / "a.nii")}]},
        {"paths": [{"value": str(rendered.run_directory / "input" / "b.nii")}]},
    ]
    assert payload["covariate_groups"] == [
        {"rows": [{"values": [20.0]}]},
        {"rows": [{"values": [30.0]}]},
    ]
    assert "dependent_paths" not in payload
    assert "other_covariates_by_group" not in payload
    assert payload["correction"]["function"] == "y_FDR_Image"
    assert payload["correction"]["parameters"]["mask_path"].endswith("input\\mask.nii")
    script = rendered.entry_script.read_text(encoding="utf-8")
    assert "y_TTest2_Image" in script
    assert "y_FDR_Image" in script
    assert "decode_path_groups(request.dependent_groups)" in script
    assert "decode_covariate_groups(request.covariate_groups)" in script
    assert "groups = cell(numel(entries), 1);" in script
    assert "covariates = cell(numel(entries), 1);" in script


def test_statistics_renderer_preserves_rectangular_group_boundaries(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    base = statistics_job()
    assert isinstance(base.payload, StatisticsJobPayload)
    call = base.payload.statistics.model_copy(
        update={
            "ordered_subject_ids": ("sub-01", "sub-02", "sub-03", "sub-04"),
            "dependent_artifact_groups": (
                ("image-1", "image-2"),
                ("image-3", "image-4"),
            ),
            "other_covariates_by_group": (
                ((20.0, 0.1), (21.0, 0.2)),
                ((30.0, 0.3), (31.0, 0.4)),
            ),
            "design_matrix": (
                (1.0, 1.0, 20.0, 0.1),
                (1.0, 1.0, 21.0, 0.2),
                (-1.0, 1.0, 30.0, 0.3),
                (-1.0, 1.0, 31.0, 0.4),
            ),
            "contrast": (1.0, 0.0, 0.0, 0.0),
        }
    )
    bindings = (
        *base.artifact_bindings,
        ArtifactPathBinding(artifact_id="image-3", relative_path="input/c.nii", read_only=True),
        ArtifactPathBinding(artifact_id="image-4", relative_path="input/d.nii", read_only=True),
    )
    rectangular = base.model_copy(
        update={
            "artifact_bindings": bindings,
            "payload": StatisticsJobPayload(statistics=call, correction=None),
        }
    )

    rendered = MatlabTemplateRenderer(Path("matlab/templates")).render(
        rectangular, environment(tmp_path), work
    )
    payload = json.loads(
        (rendered.run_directory / "config" / "statistics.json").read_text(encoding="utf-8")
    )

    assert [
        [item["value"] for item in group["paths"]] for group in payload["dependent_groups"]
    ] == [
        [
            str(rendered.run_directory / "input" / "a.nii"),
            str(rendered.run_directory / "input" / "b.nii"),
        ],
        [
            str(rendered.run_directory / "input" / "c.nii"),
            str(rendered.run_directory / "input" / "d.nii"),
        ],
    ]
    assert payload["covariate_groups"] == [
        {"rows": [{"values": [20.0, 0.1]}, {"values": [21.0, 0.2]}]},
        {"rows": [{"values": [30.0, 0.3]}, {"values": [31.0, 0.4]}]},
    ]


def test_statistics_payload_rejects_mask_and_tail_mismatch() -> None:
    wrong_mask = fdr_correction().model_copy(
        update={
            "parameters": {
                **fdr_correction().parameters,
                "mask_artifact_id": "other-mask",
            }
        }
    )
    with pytest.raises(ValidationError, match="same frozen mask"):
        StatisticsJobPayload(
            statistics=statistics_job().payload.statistics,  # type: ignore[union-attr]
            correction=wrong_mask,
        )
    one_sided = statistics_job().payload.statistics.model_copy(  # type: ignore[union-attr]
        update={"tail": "one_sided_positive"}
    )
    with pytest.raises(ValidationError, match="two-sided"):
        StatisticsJobPayload(statistics=one_sided, correction=fdr_correction())

    grf = CorrectionCall(
        function=CorrectionFunction.GRF,
        parameters={
            "voxel_p_threshold": 0.001,
            "cluster_p_threshold": 0.05,
            "mask_artifact_id": "mask",
            "statistic_type": "T",
            "df1": 1.0,
            "df2": None,
            "two_tailed": False,
            "smoothness_mode": "dpabi_header_or_estimate",
            "smoothness_dlh": None,
        },
        correction_hash="g" * 64,
    )
    with pytest.raises(ValidationError, match="two_tailed"):
        StatisticsJobPayload(
            statistics=statistics_job().payload.statistics,  # type: ignore[union-attr]
            correction=grf,
        )


def test_statistics_payload_rejects_map_type_and_df_mismatch() -> None:
    wrong_type = fdr_correction().model_copy(
        update={"parameters": {**fdr_correction().parameters, "statistic_type": "Z"}}
    )
    with pytest.raises(ValidationError, match="produce T maps"):
        StatisticsJobPayload(
            statistics=statistics_job().payload.statistics,  # type: ignore[union-attr]
            correction=wrong_type,
        )
    wrong_df = fdr_correction().model_copy(
        update={"parameters": {**fdr_correction().parameters, "df1": 2.0}}
    )
    with pytest.raises(ValidationError, match="residual DF"):
        StatisticsJobPayload(
            statistics=statistics_job().payload.statistics,  # type: ignore[union-attr]
            correction=wrong_df,
        )


def test_job_contract_rejects_kind_duplicate_binding_and_missing_base() -> None:
    values = job().model_dump()
    values["kind"] = "dpabi_statistics"
    with pytest.raises(ValidationError, match="statistics job"):
        MatlabJobSpec.model_validate(values)

    values = job().model_dump()
    values["artifact_bindings"] = (values["artifact_bindings"][0],) * 2
    with pytest.raises(ValidationError, match="unique"):
        MatlabJobSpec.model_validate(values)

    values = job().model_dump()
    values["artifact_bindings"] = ()
    with pytest.raises(ValidationError, match="not bound"):
        MatlabJobSpec.model_validate(values)


def test_renderer_refuses_unbound_artifact_and_changed_run_file(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    renderer = MatlabTemplateRenderer(Path("matlab/templates"))
    with pytest.raises(MatlabExecutionError, match="not bound"):
        renderer.render(
            statistics_job().model_copy(update={"artifact_bindings": ()}),
            environment(tmp_path),
            work,
        )

    base = work / "run-001" / "input" / "base_cfg.mat"
    base.parent.mkdir(parents=True)
    base.write_bytes(b"base")
    rendered = renderer.render(job(), environment(tmp_path), work)
    rendered.entry_script.write_text("changed", encoding="utf-8")
    with pytest.raises(MatlabExecutionError, match="refusing to overwrite"):
        renderer.render(job(), environment(tmp_path), work)


def test_template_and_path_guards_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(MatlabExecutionError, match="missing required token"):
        _replace_tokens("plain", {"TOKEN": "value"})
    with pytest.raises(MatlabExecutionError, match="unresolved"):
        _replace_tokens("{{A}} {{B}}", {"A": "value"})
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(MatlabExecutionError, match="escapes"):
        _assert_within(tmp_path / "outside", root)
    stable = root / "stable.txt"
    _write_stable(stable, "same")
    _write_stable(stable, "same")
    with pytest.raises(MatlabExecutionError, match="overwrite"):
        _write_stable(stable, "different")


class FakeProcess:
    def __init__(
        self,
        *_args: Any,
        cwd: Path,
        return_code: int = 0,
        polls_before_exit: int = 0,
        create_output: bool = False,
        **_kwargs: Any,
    ) -> None:
        self.cwd = Path(cwd)
        self.returncode: int | None = None
        self.pid = 1234
        self._return_code = return_code
        self._polls = polls_before_exit
        self._create_output = create_output
        self.communicate_timeout: float | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        if self._polls:
            self._polls -= 1
            return None
        self.returncode = self._return_code
        return self.returncode

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self.communicate_timeout = timeout
        if self._create_output:
            output = self.cwd / "output" / "Results" / "ALFF_test" / "ALFFMap_sub-01.nii"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"synthetic")
        return "stdout", "stderr" if self._return_code else ""

    def terminate(self) -> None:
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            self.returncode = self._return_code
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def executable_environment(tmp_path: Path) -> MatlabEnvironment:
    env = environment(tmp_path)
    env.matlab_executable.parent.mkdir(parents=True)
    env.matlab_executable.write_bytes(b"not-executed")
    return env


def prepared_work(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    base = work / "run-001" / "input" / "base_cfg.mat"
    base.parent.mkdir(parents=True)
    base.write_bytes(b"base")
    return work


def test_controlled_executor_maps_success_missing_output_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = prepared_work(tmp_path)
    executor = ControlledMatlabExecutor(
        MatlabTemplateRenderer(Path("matlab/templates")),
        executable_environment(tmp_path),
        work,
        allow_real_execution=True,
    )
    monkeypatch.setattr(
        matlab_module.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(*args, **kwargs, create_output=True),
    )
    success = executor.execute(job(), is_cancelled=lambda: False)
    assert success.status.value == "succeeded"
    assert success.exit_code == 0
    assert success.registered_artifacts
    assert success.registered_artifacts[0].relative_path.endswith("ALFFMap_sub-01.nii")
    assert success.registered_artifacts[0].size_bytes == len(b"synthetic")
    assert len(success.registered_artifacts[0].sha256) == 64
    assert success.stdout == "stdout"

    work_missing = prepared_work(tmp_path / "missing")
    missing_executor = ControlledMatlabExecutor(
        MatlabTemplateRenderer(Path("matlab/templates")),
        executable_environment(tmp_path / "missing"),
        work_missing,
        allow_real_execution=True,
    )
    monkeypatch.setattr(
        matlab_module.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(*args, **kwargs),
    )
    missing = missing_executor.execute(job(), is_cancelled=lambda: False)
    assert missing.status.value == "failed"
    assert "Missing fresh non-empty artifacts" in missing.stderr

    work_failed = prepared_work(tmp_path / "failed")
    failed_executor = ControlledMatlabExecutor(
        MatlabTemplateRenderer(Path("matlab/templates")),
        executable_environment(tmp_path / "failed"),
        work_failed,
        allow_real_execution=True,
    )
    monkeypatch.setattr(
        matlab_module.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(*args, **kwargs, return_code=7),
    )
    failed = failed_executor.execute(job(), is_cancelled=lambda: False)
    assert failed.status.value == "failed"
    assert failed.exit_code == 7


@pytest.mark.parametrize("stale_kind", ["nonempty", "empty", "directory"])
def test_controlled_executor_rejects_stale_empty_and_directory_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale_kind: str
) -> None:
    work = prepared_work(tmp_path)
    output = work / "run-001" / "output" / "Results" / "ALFF_old" / "ALFFMap_sub-01.nii"
    output.parent.mkdir(parents=True)
    if stale_kind == "nonempty":
        output.write_bytes(b"stale")
    elif stale_kind == "empty":
        output.touch()
    else:
        output.mkdir()
    executor = ControlledMatlabExecutor(
        MatlabTemplateRenderer(Path("matlab/templates")),
        executable_environment(tmp_path),
        work,
        allow_real_execution=True,
    )
    monkeypatch.setattr(
        matlab_module.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(*args, **kwargs),
    )

    result = executor.execute(job(), is_cancelled=lambda: False)

    assert result.status.value == "failed"
    assert result.registered_artifacts == ()
    assert "Missing fresh non-empty artifacts" in result.stderr


def test_controlled_executor_retry_cannot_reuse_failed_attempt_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = prepared_work(tmp_path)
    executor = ControlledMatlabExecutor(
        MatlabTemplateRenderer(Path("matlab/templates")),
        executable_environment(tmp_path),
        work,
        allow_real_execution=True,
    )
    processes = iter(
        (
            FakeProcess([], cwd=work / "run-001", return_code=7, create_output=True),
            FakeProcess([], cwd=work / "run-001"),
        )
    )
    monkeypatch.setattr(matlab_module.subprocess, "Popen", lambda *_a, **_kw: next(processes))

    failed = executor.execute(job(), is_cancelled=lambda: False)
    retry = executor.execute(job(), is_cancelled=lambda: False)

    assert failed.status.value == "failed"
    assert retry.status.value == "failed"
    assert retry.registered_artifacts == ()
    assert "Missing fresh non-empty artifacts" in retry.stderr


def test_controlled_executor_maps_cancel_timeout_and_missing_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = prepared_work(tmp_path)
    missing_env = environment(tmp_path)
    executor = ControlledMatlabExecutor(
        MatlabTemplateRenderer(Path("matlab/templates")),
        missing_env,
        work,
        allow_real_execution=True,
    )
    with pytest.raises(MatlabExecutionError, match="not found"):
        executor.execute(job(), is_cancelled=lambda: False)

    env = executable_environment(tmp_path)
    executor = ControlledMatlabExecutor(
        MatlabTemplateRenderer(Path("matlab/templates")),
        env,
        work,
        allow_real_execution=True,
    )
    process = FakeProcess([], cwd=work / "run-001", polls_before_exit=10)
    monkeypatch.setattr(matlab_module.subprocess, "Popen", lambda *_a, **_kw: process)
    monkeypatch.setattr(matlab_module, "_terminate_process_tree", lambda item: item.terminate())
    cancelled = executor.execute(job(), is_cancelled=lambda: True)
    assert cancelled.status.value == "cancelled"

    timeout_work = prepared_work(tmp_path / "timeout")
    timeout_executor = ControlledMatlabExecutor(
        MatlabTemplateRenderer(Path("matlab/templates")),
        executable_environment(tmp_path / "timeout"),
        timeout_work,
        allow_real_execution=True,
    )
    timeout_job = job().model_copy(update={"timeout_seconds": 1})
    process = FakeProcess([], cwd=timeout_work / "run-001", polls_before_exit=10)
    monkeypatch.setattr(matlab_module.subprocess, "Popen", lambda *_a, **_kw: process)
    moments = iter((0.0, 2.0))
    monkeypatch.setattr(matlab_module.time, "monotonic", lambda: next(moments))
    timed_out = timeout_executor.execute(timeout_job, is_cancelled=lambda: False)
    assert timed_out.status.value == "timed_out"


def test_controlled_executor_streams_large_output_to_bounded_local_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = prepared_work(tmp_path)
    executor = ControlledMatlabExecutor(
        MatlabTemplateRenderer(Path("matlab/templates")),
        executable_environment(tmp_path),
        work,
        allow_real_execution=True,
    )

    def file_stream_process(*args: Any, **kwargs: Any) -> FakeProcess:
        assert kwargs["stdout"] is not matlab_module.subprocess.PIPE
        assert kwargs["stderr"] is not matlab_module.subprocess.PIPE
        kwargs["stdout"].write("x" * (1024 * 1024 + 4096))
        kwargs["stderr"].write("synthetic warning")
        kwargs["stdout"].flush()
        kwargs["stderr"].flush()
        return FakeProcess(*args, **kwargs, create_output=True)

    monkeypatch.setattr(matlab_module.subprocess, "Popen", file_stream_process)
    result = executor.execute(job(), is_cancelled=lambda: False)

    assert result.status.value == "succeeded"
    assert result.stdout.startswith("[output truncated")
    assert result.stdout.endswith("stdout")
    assert result.stderr == "synthetic warning"
    first_logs = sorted((work / "run-001" / "logs" / "job-001").glob("attempt-*/matlab.stdout.log"))
    assert len(first_logs) == 1
    assert first_logs[0].stat().st_size > 1024 * 1024

    executor.execute(job(), is_cancelled=lambda: False)
    all_logs = sorted((work / "run-001" / "logs" / "job-001").glob("attempt-*/matlab.stdout.log"))
    assert len(all_logs) == 2
    assert first_logs[0].read_text(encoding="utf-8").endswith("stdout")


def test_process_tree_termination_is_bounded_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubbornProcess(FakeProcess):
        killed = False

        def wait(self, timeout: float | None = None) -> int:
            if not self.killed:
                raise matlab_module.subprocess.TimeoutExpired("matlab", timeout)
            assert self.returncode is not None
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

    process = StubbornProcess([], cwd=Path.cwd(), polls_before_exit=100)

    def timed_out_taskkill(*_args: Any, **_kwargs: Any) -> None:
        raise matlab_module.subprocess.TimeoutExpired("taskkill", 10)

    monkeypatch.setattr(matlab_module.subprocess, "run", timed_out_taskkill)

    with pytest.raises(MatlabExecutionError, match="process-tree termination"):
        matlab_module._terminate_process_tree(process)

    assert process.killed is True
    assert process.returncode == -9


def test_process_log_finalization_timeout_fails_without_unbounded_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = prepared_work(tmp_path)
    executor = ControlledMatlabExecutor(
        MatlabTemplateRenderer(Path("matlab/templates")),
        executable_environment(tmp_path),
        work,
        allow_real_execution=True,
    )
    process = FakeProcess([], cwd=work / "run-001", create_output=True)

    def blocked_communicate(timeout: float | None = None) -> tuple[str, str]:
        raise matlab_module.subprocess.TimeoutExpired("matlab", timeout)

    process.communicate = blocked_communicate  # type: ignore[method-assign]
    monkeypatch.setattr(matlab_module.subprocess, "Popen", lambda *_a, **_kw: process)

    with pytest.raises(MatlabExecutionError, match="log finalization"):
        executor.execute(job(), is_cancelled=lambda: False)


def test_mock_executor_covers_success_failure_and_cancellation(tmp_path: Path) -> None:
    work = prepared_work(tmp_path)
    rendered = MatlabTemplateRenderer(Path("matlab/templates")).render(
        job(), environment(tmp_path), work
    )
    executor = MockMatlabExecutor()
    assert executor.execute(job(), rendered, is_cancelled=lambda: False).status.value == "succeeded"
    assert (
        executor.execute(job(), rendered, is_cancelled=lambda: False, fail=True).status.value
        == "failed"
    )
    assert executor.execute(job(), rendered, is_cancelled=lambda: True).status.value == "cancelled"
