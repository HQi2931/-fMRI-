"""Stable transport-neutral values used by the persistent workflow worker."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator


class JobKind(StrEnum):
    """Stable, version-independent names for registered worker capabilities."""

    WORKFLOW_MOCK = "workflow.mock"
    MATLAB_PREPROCESSING = "matlab.preprocessing"
    MATLAB_STATISTICS = "matlab.statistics"


_LEGACY_EXECUTOR_KINDS = {
    "workflow_mock": JobKind.WORKFLOW_MOCK,
    "matlab_preprocessing": JobKind.MATLAB_PREPROCESSING,
    "matlab_statistics": JobKind.MATLAB_STATISTICS,
}
_KIND_LEGACY_EXECUTORS = {kind: legacy for legacy, kind in _LEGACY_EXECUTOR_KINDS.items()}


class JobEnvelope(BaseModel):
    """Validated transport contract persisted in the job queue.

    ``command`` contains the controlled executor inputs. Approval and execution
    policy bindings are kept outside it so they can be inspected and checked
    before dispatch. SQLite rows written before v1 are converted by
    :meth:`from_persisted` at the queue boundary.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    job_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    kind: JobKind
    command: dict[str, Any]
    approval_binding: dict[str, Any] = Field(default_factory=dict)
    execution_policy: dict[str, Any] = Field(default_factory=dict)
    trace_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_command_binding(self) -> JobEnvelope:
        if self.kind in {JobKind.MATLAB_PREPROCESSING, JobKind.MATLAB_STATISTICS}:
            if self.execution_policy.get("execution_backend") != "matlab":
                raise ValueError("MATLAB job kind requires a MATLAB execution policy")
            if self.execution_policy.get("real_execution_confirmed") is not True:
                raise ValueError("MATLAB job kind requires explicit real-execution confirmation")
            if not self.approval_binding.get("plan_hash"):
                raise ValueError("MATLAB job requires an approved plan hash binding")
            if not self.approval_binding.get("input_manifest_hash"):
                raise ValueError("MATLAB job requires a frozen manifest hash binding")
        if self.kind is JobKind.MATLAB_STATISTICS:
            if self.command.get("run_kind") != "statistics_matlab":
                raise ValueError("MATLAB statistics kind requires a statistics command")
            if not isinstance(self.command.get("statistical_design"), dict):
                raise ValueError("MATLAB statistics command requires a frozen design")
        return self

    @classmethod
    def from_persisted(
        cls,
        payload: Any,
        *,
        job_id: str,
        run_id: str,
        attempt: int,
    ) -> JobEnvelope:
        """Decode v1 data or adapt a legacy ``executor_type`` payload."""

        if not isinstance(payload, dict):
            raise ValueError("persisted job payload must be an object")
        if "schema_version" in payload:
            envelope = cls.model_validate(payload)
            return envelope.model_copy(
                update={"job_id": job_id, "run_id": run_id, "attempt": attempt}
            )

        legacy_kind = payload.get("executor_type", "workflow_mock")
        try:
            kind = _LEGACY_EXECUTOR_KINDS[str(legacy_kind)]
        except KeyError:
            raise ValueError(f"unsupported legacy executor type: {legacy_kind}") from None
        command = {key: value for key, value in payload.items() if key != "executor_type"}
        approval_keys = {
            "plan_hash",
            "input_manifest_hash",
            "environment_hash",
            "approval_record_id",
        }
        policy_keys = {
            "execution_backend",
            "workspace_mode",
            "real_execution_confirmed",
            "qc_mode",
        }
        return cls(
            job_id=job_id,
            run_id=run_id,
            attempt=attempt,
            kind=kind,
            command=command,
            approval_binding={key: command[key] for key in approval_keys if key in command},
            execution_policy={key: command[key] for key in policy_keys if key in command},
            trace_id=run_id[:36],
        )

    def executor_payload(self) -> dict[str, Any]:
        """Flatten the validated envelope for the existing controlled adapters."""

        payload = dict(self.command)
        payload.update(self.approval_binding)
        payload.update(self.execution_policy)
        payload.update(
            executor_type=_KIND_LEGACY_EXECUTORS[self.kind],
            job_id=self.job_id,
            run_id=self.run_id,
            attempt=self.attempt,
            trace_id=self.trace_id,
        )
        return payload


class ClaimedJob(TypedDict):
    job_id: str
    run_id: str
    payload: dict[str, Any]
    attempt: int
    max_attempts: int
