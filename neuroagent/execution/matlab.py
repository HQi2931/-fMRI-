"""Deterministic MATLAB template rendering and opt-in controlled execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from neuroagent.execution.models import (
    MatlabEnvironment,
    MatlabJobKind,
    MatlabJobResult,
    MatlabJobSpec,
    MatlabJobStatus,
    PreprocessingJobPayload,
    RenderedJob,
    StatisticsJobPayload,
    VerifiedArtifact,
)


class MatlabExecutionError(RuntimeError):
    pass


class MatlabExecutionNotAuthorized(MatlabExecutionError):
    pass


_MAX_INLINE_LOG_BYTES = 1024 * 1024
_TERMINATION_TIMEOUT_SECONDS = 10.0
_COMMUNICATE_TIMEOUT_SECONDS = 10.0


class MatlabTemplateRenderer:
    def __init__(self, template_root: Path) -> None:
        self._template_root = template_root.resolve(strict=True)

    def render(
        self,
        job: MatlabJobSpec,
        environment: MatlabEnvironment,
        allowed_work_root: Path,
    ) -> RenderedJob:
        work_root = allowed_work_root.resolve(strict=True)
        run_directory = (work_root / job.run_id).resolve()
        _assert_within(run_directory, work_root)
        run_directory.mkdir(parents=True, exist_ok=True)
        directories = (
            "config",
            "scripts",
            "logs",
            "output",
            "qc",
            "input",
            "staging",
        )
        for directory in directories:
            (run_directory / directory).mkdir(exist_ok=True)

        bootstrap = self._load_template("bootstrap.m.tmpl")
        bootstrap = _replace_tokens(
            bootstrap,
            {
                "DPABI_PATH": _matlab_quote(environment.dpabi_path.resolve()),
                "SPM_PATH": _matlab_quote(environment.spm_path.resolve()),
            },
        )
        bootstrap_path = run_directory / "scripts" / "bootstrap.m"
        _write_stable(bootstrap_path, bootstrap)

        generated = ["scripts/bootstrap.m"]
        if job.kind is MatlabJobKind.DPARSFA_PREPROCESSING:
            assert isinstance(job.payload, PreprocessingJobPayload)
            payload_path = run_directory / "config" / "preprocessing.json"
            _write_stable(
                payload_path,
                json.dumps(
                    {
                        "overrides": job.payload.metric_projection.cfg,
                        "base_cfg_allowed_fields": job.payload.base_cfg_allowed_fields,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
            )
            subject_list = run_directory / "subject_list.txt"
            _write_stable(subject_list, "\n".join(job.payload.subject_ids) + "\n")
            base_cfg = self._resolve_artifact(run_directory, job, job.payload.base_cfg_artifact_id)
            staging = (run_directory / job.payload.staging_relative_path).resolve()
            _assert_within(staging, run_directory)
            script = _replace_tokens(
                self._load_template("run_preprocessing.m.tmpl"),
                {
                    "BOOTSTRAP_PATH": _matlab_quote(bootstrap_path),
                    "RUN_DIRECTORY": _matlab_quote(run_directory),
                    "BASE_CFG_PATH": _matlab_quote(base_cfg),
                    "STAGING_DIRECTORY": _matlab_quote(staging),
                    "SUBJECT_LIST_PATH": _matlab_quote(subject_list),
                    "CONFIG_JSON_PATH": _matlab_quote(payload_path),
                },
            )
            entry_path = run_directory / "scripts" / "run_preprocessing.m"
            generated.extend(
                (
                    "config/preprocessing.json",
                    "subject_list.txt",
                    "scripts/run_preprocessing.m",
                )
            )
        else:
            assert isinstance(job.payload, StatisticsJobPayload)
            request = self._materialize_statistics_payload(run_directory, job, job.payload)
            payload_path = run_directory / "config" / "statistics.json"
            _write_stable(
                payload_path,
                json.dumps(request, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            )
            script = _replace_tokens(
                self._load_template("run_statistics.m.tmpl"),
                {
                    "BOOTSTRAP_PATH": _matlab_quote(bootstrap_path),
                    "RUN_DIRECTORY": _matlab_quote(run_directory),
                    "CONFIG_JSON_PATH": _matlab_quote(payload_path),
                },
            )
            entry_path = run_directory / "scripts" / "run_statistics.m"
            generated.extend(("config/statistics.json", "scripts/run_statistics.m"))
        _write_stable(entry_path, script)

        provenance = {
            "job_hash": job.job_hash,
            "plan_hash": job.plan_hash,
            "approval_record_id": job.approval_record_id,
            "input_manifest_hash": job.input_manifest_hash,
            "matlab": environment.matlab_version,
            "spm": environment.spm_version,
            "dpabi": environment.dpabi_version,
        }
        _write_stable(
            run_directory / "provenance.json",
            json.dumps(provenance, sort_keys=True, indent=2) + "\n",
        )
        generated.append("provenance.json")
        command = (
            str(environment.matlab_executable.resolve()),
            "-batch",
            f"run('{_matlab_quote(entry_path)}')",
        )
        return RenderedJob(
            run_directory=run_directory,
            entry_script=entry_path,
            command=command,
            job_hash=job.job_hash,
            generated_files=tuple(generated),
        )

    def _load_template(self, name: str) -> str:
        path = (self._template_root / name).resolve(strict=True)
        _assert_within(path, self._template_root)
        return path.read_text(encoding="utf-8")

    def _resolve_artifact(self, run_directory: Path, job: MatlabJobSpec, artifact_id: str) -> Path:
        binding = next(
            (item for item in job.artifact_bindings if item.artifact_id == artifact_id), None
        )
        if binding is None:
            raise MatlabExecutionError(f"artifact {artifact_id!r} is not bound")
        resolved = (run_directory / binding.relative_path).resolve()
        _assert_within(resolved, run_directory)
        return resolved

    def _materialize_statistics_payload(
        self,
        run_directory: Path,
        job: MatlabJobSpec,
        payload: StatisticsJobPayload,
    ) -> dict[str, object]:
        call = payload.statistics
        dependent_groups = [
            {
                "paths": [
                    {"value": str(self._resolve_artifact(run_directory, job, artifact_id))}
                    for artifact_id in group
                ]
            }
            for group in call.dependent_artifact_groups
        ]
        covariate_groups = [
            {"rows": [{"values": list(row)} for row in group]}
            for group in call.other_covariates_by_group
        ]
        mask_path = str(self._resolve_artifact(run_directory, job, call.mask_artifact_id))
        request: dict[str, object] = {
            "function": call.function.value,
            "ordered_subject_ids": call.ordered_subject_ids,
            "dependent_groups": dependent_groups,
            "covariate_groups": covariate_groups,
            "mask_path": mask_path,
            "design_matrix": call.design_matrix,
            "contrast": call.contrast,
            "one_sample_baseline": call.one_sample_baseline,
            "tail": call.tail,
            "residual_df": call.residual_df,
            "revision_id": call.revision_id,
            "design_hash": call.design_hash,
        }
        if payload.correction is not None:
            parameters = dict(payload.correction.parameters)
            mask_id = parameters.pop("mask_artifact_id")
            parameters["mask_path"] = str(self._resolve_artifact(run_directory, job, str(mask_id)))
            request["correction"] = {
                "function": payload.correction.function.value,
                "parameters": parameters,
                "correction_hash": payload.correction.correction_hash,
            }
        else:
            request["correction"] = None
        return request


class ControlledMatlabExecutor:
    """Run only rendered, approved JobSpecs when explicitly enabled by the caller."""

    def __init__(
        self,
        renderer: MatlabTemplateRenderer,
        environment: MatlabEnvironment,
        allowed_work_root: Path,
        *,
        allow_real_execution: bool = False,
    ) -> None:
        self._renderer = renderer
        self._environment = environment
        self._allowed_work_root = allowed_work_root
        self._allow_real_execution = allow_real_execution

    def dry_run(self, job: MatlabJobSpec) -> MatlabJobResult:
        rendered = self._renderer.render(job, self._environment, self._allowed_work_root)
        return MatlabJobResult(
            status=MatlabJobStatus.DRY_RUN,
            job_hash=job.job_hash,
            exit_code=None,
            stdout="",
            stderr="",
            rendered=rendered,
            registered_artifacts=(),
        )

    def execute(self, job: MatlabJobSpec, *, is_cancelled: Callable[[], bool]) -> MatlabJobResult:
        if not self._allow_real_execution:
            raise MatlabExecutionNotAuthorized(
                "real MATLAB execution requires explicit caller authorization"
            )
        rendered = self._renderer.render(job, self._environment, self._allowed_work_root)
        executable = Path(rendered.command[0])
        if not executable.is_file():
            raise MatlabExecutionError(f"MATLAB executable not found: {executable}")
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        stdout_path, stderr_path = _allocate_execution_logs(rendered.run_directory, job.job_id)
        prior_artifacts = _snapshot_expected_artifacts(job, rendered.run_directory)
        requested_status: MatlabJobStatus | None = None
        with (
            stdout_path.open("w", encoding="utf-8", newline="") as stdout_log,
            stderr_path.open("w", encoding="utf-8", newline="") as stderr_log,
        ):
            process = subprocess.Popen(
                list(rendered.command),
                cwd=rendered.run_directory,
                stdout=stdout_log,
                stderr=stderr_log,
                text=True,
                shell=False,
                creationflags=creation_flags,
            )
            deadline = time.monotonic() + job.timeout_seconds
            while process.poll() is None:
                if is_cancelled():
                    requested_status = MatlabJobStatus.CANCELLED
                    _terminate_process_tree(process)
                    break
                if time.monotonic() >= deadline:
                    requested_status = MatlabJobStatus.TIMED_OUT
                    _terminate_process_tree(process)
                    break
                time.sleep(0.1)
            # Real Popen returns (None, None) because output is streamed to
            # files. Accept strings as a compatibility seam for deterministic
            # fake processes used by tests.
            try:
                communicated_stdout, communicated_stderr = process.communicate(
                    timeout=_COMMUNICATE_TIMEOUT_SECONDS
                )
            except subprocess.TimeoutExpired as exc:
                raise MatlabExecutionError(
                    "MATLAB process log finalization exceeded the bounded timeout"
                ) from exc
            _append_communicated_output(stdout_log, communicated_stdout)
            _append_communicated_output(stderr_log, communicated_stderr)

        stdout = _read_bounded_log(stdout_path)
        stderr = _read_bounded_log(stderr_path)
        if requested_status is not None:
            return _result(job, rendered, requested_status, stdout, stderr, process.returncode, ())
        status = MatlabJobStatus.SUCCEEDED if process.returncode == 0 else MatlabJobStatus.FAILED
        registered_artifacts: tuple[VerifiedArtifact, ...] = ()
        if status is MatlabJobStatus.SUCCEEDED:
            registered_artifacts, missing = _verify_fresh_artifacts(
                job, rendered.run_directory, prior_artifacts
            )
            if missing:
                status = MatlabJobStatus.FAILED
                registered_artifacts = ()
                stderr = (
                    f"{stderr}\nMissing fresh non-empty artifacts: {', '.join(missing)}"
                ).strip()
        return _result(
            job,
            rendered,
            status,
            stdout,
            stderr,
            process.returncode,
            registered_artifacts,
        )


def _result(
    job: MatlabJobSpec,
    rendered: RenderedJob,
    status: MatlabJobStatus,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    artifacts: tuple[VerifiedArtifact, ...],
) -> MatlabJobResult:
    return MatlabJobResult(
        status=status,
        job_hash=job.job_hash,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        rendered=rendered,
        registered_artifacts=artifacts,
    )


def _append_communicated_output(stream: TextIO, output: str | bytes | None) -> None:
    if output is None:
        return
    text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output
    if text:
        stream.write(text)
        stream.flush()


def _allocate_execution_logs(run_directory: Path, job_id: str) -> tuple[Path, Path]:
    job_log_root = run_directory / "logs" / job_id
    job_log_root.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, 10_000):
        attempt_directory = job_log_root / f"attempt-{attempt:03d}"
        try:
            attempt_directory.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return (
            attempt_directory / "matlab.stdout.log",
            attempt_directory / "matlab.stderr.log",
        )
    raise MatlabExecutionError(f"too many preserved execution logs for job {job_id!r}")


def _read_bounded_log(path: Path) -> str:
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        if size > _MAX_INLINE_LOG_BYTES:
            stream.seek(-_MAX_INLINE_LOG_BYTES, os.SEEK_END)
            content = stream.read()
            return (
                f"[output truncated to the final {_MAX_INLINE_LOG_BYTES} bytes; "
                f"full log: {path.name}]\n" + content.decode("utf-8", errors="replace")
            )
        stream.seek(0)
        return stream.read().decode("utf-8", errors="replace")


def _snapshot_expected_artifacts(
    job: MatlabJobSpec, run_directory: Path
) -> dict[str, tuple[int, int, str]]:
    snapshot: dict[str, tuple[int, int, str]] = {}
    for expected in job.expected_artifacts:
        for path in _matching_regular_files(run_directory, expected.relative_pattern):
            relative_path = path.relative_to(run_directory.resolve(strict=True)).as_posix()
            fingerprint = _artifact_fingerprint(path)
            if fingerprint is not None:
                snapshot[relative_path] = fingerprint
    return snapshot


def _verify_fresh_artifacts(
    job: MatlabJobSpec,
    run_directory: Path,
    prior_artifacts: dict[str, tuple[int, int, str]],
) -> tuple[tuple[VerifiedArtifact, ...], tuple[str, ...]]:
    verified: list[VerifiedArtifact] = []
    missing: list[str] = []
    run_root = run_directory.resolve(strict=True)
    for expected in job.expected_artifacts:
        fresh_for_contract = 0
        for path in _matching_regular_files(run_directory, expected.relative_pattern):
            relative_path = path.relative_to(run_root).as_posix()
            fingerprint = _artifact_fingerprint(path)
            if fingerprint is None or prior_artifacts.get(relative_path) == fingerprint:
                continue
            fresh_for_contract += 1
            verified.append(
                VerifiedArtifact(
                    artifact_type=expected.artifact_type,
                    relative_path=relative_path,
                    size_bytes=fingerprint[0],
                    sha256=fingerprint[2],
                )
            )
        if expected.required and fresh_for_contract == 0:
            missing.append(expected.relative_pattern)
    return tuple(verified), tuple(missing)


def _matching_regular_files(run_directory: Path, pattern: str) -> tuple[Path, ...]:
    run_root = run_directory.resolve(strict=True)
    matches: list[Path] = []
    for candidate in run_directory.glob(pattern):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        resolved = candidate.resolve(strict=True)
        _assert_within(resolved, run_root)
        matches.append(resolved)
    return tuple(sorted(matches))


def _artifact_fingerprint(path: Path) -> tuple[int, int, str] | None:
    before = path.stat()
    if before.st_size <= 0:
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise MatlabExecutionError(f"artifact changed while it was being verified: {path.name}")
    return after.st_size, after.st_mtime_ns, digest.hexdigest()


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        tree_termination_verified = False
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                shell=False,
                timeout=_TERMINATION_TIMEOUT_SECONDS,
            )
            tree_termination_verified = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            tree_termination_verified = False
    else:
        tree_termination_verified = True
        try:
            process.terminate()
        except OSError:
            if process.poll() is None:
                raise MatlabExecutionError("failed to request MATLAB process termination") from None

    try:
        process.wait(timeout=_TERMINATION_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            if process.poll() is None:
                raise MatlabExecutionError("failed to force-stop the MATLAB process") from None
        try:
            process.wait(timeout=_TERMINATION_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise MatlabExecutionError(
                "MATLAB process remained alive after bounded termination escalation"
            ) from exc

    if process.poll() is None:
        raise MatlabExecutionError("MATLAB process termination could not be confirmed")
    if os.name == "nt" and not tree_termination_verified:
        raise MatlabExecutionError(
            "MATLAB root process stopped but process-tree termination could not be verified"
        )


def _replace_tokens(template: str, replacements: dict[str, str]) -> str:
    rendered = template
    for name, value in replacements.items():
        token = "{{" + name + "}}"
        if token not in rendered:
            raise MatlabExecutionError(f"template is missing required token {token}")
        rendered = rendered.replace(token, value)
    if re.search(r"\{\{[A-Z0-9_]+\}\}", rendered):
        raise MatlabExecutionError("template contains unresolved tokens")
    return rendered


def _matlab_quote(path: Path) -> str:
    return str(path).replace("'", "''").replace("\\", "/")


def _write_stable(path: Path, content: str) -> None:
    encoded = content.encode("utf-8")
    if path.exists():
        existing = path.read_bytes()
        if existing != encoded:
            raise MatlabExecutionError(f"refusing to overwrite different run artifact: {path}")
        return
    path.write_bytes(encoded)


def _assert_within(candidate: Path, root: Path) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise MatlabExecutionError(f"path escapes allowed root: {candidate}") from exc
