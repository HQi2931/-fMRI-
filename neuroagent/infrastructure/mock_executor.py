"""Deterministic executor used by tests and the initial vertical slice."""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from neuroagent.application.hashing import content_hash
from neuroagent.execution.ports import ExecutionResult, MockExecutionCatalog
from neuroagent.skills.models import ResolvedStep, SkillPlan, stable_hash
from neuroagent.workflow.runtime import (
    RuntimeArtifact,
    ToolRuntime,
    ToolStepResult,
    WorkflowFactory,
)


class _SyntheticToolExecutor:
    def __init__(self, manifest_hash: str = "0" * 64) -> None:
        self._manifest_hash = manifest_hash

    def execute(
        self,
        step: ResolvedStep,
        inputs: tuple[RuntimeArtifact, ...],
        *,
        attempt: int,
        is_cancelled: Callable[[], bool],
    ) -> ToolStepResult:
        if is_cancelled():
            raise RuntimeError("mock workflow cancelled")
        manifest_hash = inputs[0].manifest_hash if inputs else self._manifest_hash
        artifacts = tuple(
            RuntimeArtifact(
                artifact_id=f"mock-{attempt}-{step.step_id}-{index}",
                artifact_type=artifact_type,
                manifest_hash=manifest_hash,
                lineage_hash=stable_hash(
                    {
                        "step": step.step_id,
                        "tool": step.tool.tool_id,
                        "attempt": attempt,
                        "artifact_type": artifact_type,
                    }
                ),
            )
            for index, artifact_type in enumerate(step.produces)
        )
        return ToolStepResult(artifacts=artifacts)


class MockJobExecutor:
    def __init__(
        self,
        workflow_factory: WorkflowFactory | None = None,
        *,
        repository: MockExecutionCatalog | None = None,
    ) -> None:
        self._workflow_factory = workflow_factory
        self._repository = repository

    def execute(
        self,
        payload: dict[str, Any],
        *,
        is_cancelled: Callable[[], bool],
    ) -> ExecutionResult:
        delay_ms = int(payload.get("delay_ms", 0))
        elapsed = 0
        while elapsed < delay_ms:
            if is_cancelled():
                return ExecutionResult(status="cancelled", error="cancel requested")
            interval = min(20, delay_ms - elapsed)
            time.sleep(interval / 1000)
            elapsed += interval
        if is_cancelled():
            return ExecutionResult(status="cancelled", error="cancel requested")
        if payload.get("executor_type", "workflow_mock") != "workflow_mock":
            return ExecutionResult(
                status="failed_terminal", error="requested executor type is not registered"
            )
        if payload.get("operation") == "t1_preparation":
            return ExecutionResult(
                status="failed_terminal", error="standalone T1 preparation is no longer supported"
            )

        workflow_payload = payload.get("workflow_plan")
        if workflow_payload is not None:
            if self._workflow_factory is None:
                return ExecutionResult(
                    status="failed_terminal", error="workflow runtime is not configured"
                )
            try:
                plan = SkillPlan.model_validate(workflow_payload)
                workflow = self._workflow_factory.from_approved_plan(
                    plan,
                    approval_plan_hash=str(payload.get("plan_hash", "")),
                    current_environment_hash=plan.environment.environment_hash,
                )
                initial = {
                    name: RuntimeArtifact(
                        artifact_id=f"input-{name}",
                        artifact_type=name,
                        manifest_hash=plan.input_manifest_hash,
                        lineage_hash=stable_hash({"input": name, "plan": plan.plan_hash}),
                    )
                    for step in workflow.ordered_steps
                    for name in step.consumes
                    if name
                    not in {
                        produced for item in workflow.ordered_steps for produced in item.produces
                    }
                }
                result = ToolRuntime().execute(
                    workflow,
                    _SyntheticToolExecutor(plan.input_manifest_hash),
                    initial_artifacts=initial,
                    is_cancelled=is_cancelled,
                )
            except Exception as exc:
                return ExecutionResult(
                    status="failed_terminal",
                    error=f"workflow validation failed: {type(exc).__name__}",
                )
            t1_artifacts: tuple[dict[str, Any], ...] = ()
            preprocessing = dict(plan.resolved_parameters).get("preprocessing")
            if isinstance(preprocessing, dict) and preprocessing.get("t1") is not None:
                prepared = self._prepare_synthetic_t1(payload, is_cancelled)
                if prepared.status != "succeeded":
                    return prepared
                t1_artifacts = prepared.artifacts
            return ExecutionResult(
                status="succeeded",
                output={
                    "executor": "workflow_mock",
                    "validated": True,
                    "completed_steps": result.completed_step_ids,
                },
                artifacts=t1_artifacts
                + tuple(
                    {
                        "artifact_type": artifact.artifact_type,
                        "relative_path": f"output/{artifact.artifact_id}.json",
                        "checksum": stable_hash(
                            artifact.__dict__
                            if hasattr(artifact, "__dict__")
                            else {
                                "artifact_id": artifact.artifact_id,
                                "artifact_type": artifact.artifact_type,
                                "manifest_hash": artifact.manifest_hash,
                                "lineage_hash": artifact.lineage_hash,
                            }
                        ),
                        "size_bytes": 1,
                        "provenance": {
                            "executor": "workflow_mock",
                            "lineage_hash": artifact.lineage_hash,
                        },
                    }
                    for artifact in result.artifacts
                ),
            )

        outcome = str(payload.get("outcome", "succeed"))
        if outcome == "fail_retryable":
            return ExecutionResult(status="failed_retryable", error="mock retryable failure")
        if outcome == "fail_terminal":
            return ExecutionResult(status="failed_terminal", error="mock terminal failure")
        if outcome == "timeout":
            return ExecutionResult(status="timed_out", error="mock timeout")
        result_payload = {"executor": "mock", "validated": True}
        checksum = content_hash(result_payload)
        return ExecutionResult(
            status="succeeded",
            output=result_payload,
            artifacts=(
                {
                    "artifact_type": "mock.result",
                    "relative_path": "output/mock-result.json",
                    "checksum": checksum,
                    "size_bytes": len(str(result_payload).encode("utf-8")),
                    "provenance": {"executor": "mock", "payload_hash": content_hash(payload)},
                },
            ),
        )

    def _prepare_synthetic_t1(
        self, payload: dict[str, Any], is_cancelled: Callable[[], bool]
    ) -> ExecutionResult:
        """A visibly non-scientific fixture; never reads or transforms source anatomy."""
        if self._repository is None:
            return ExecutionResult(
                status="failed_terminal", error="mock preparation repository missing"
            )
        outcome = str(payload.get("outcome", "succeed"))
        if outcome != "succeed":
            status = {
                "fail_retryable": "failed_retryable",
                "fail_terminal": "failed_terminal",
                "timeout": "timed_out",
            }.get(outcome, "failed_terminal")
            return ExecutionResult(status=status, error="mock T1 preparation failure")
        run = self._repository.get_run(str(payload["run_id"]))
        plan = self._repository.get_plan(run.plan_revision_id)
        self._repository.get_approved_plan_approval(run.plan_revision_id)
        manifest = self._repository.get_manifest(str(plan.plan["dataset_manifest_id"]))
        project = self._repository.get_project(run.project_id)
        attempts = Path(project.work_root) / run.run_id / "attempts"
        attempts.mkdir(parents=True, exist_ok=True)
        attempt = Path(tempfile.mkdtemp(prefix="attempt-mock-t1-", dir=attempts))
        artifacts: list[dict[str, Any]] = []
        outputs = []
        affine = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
        metadata = {
            "dimensions": [2, 2, 2],
            "voxel_size_mm": [1, 1, 1],
            "volume_count": 1,
            "affine": affine,
        }
        for subject in manifest.subjects:
            if is_cancelled():
                return ExecutionResult(status="cancelled", error="mock preparation cancelled")
            # The real compiler enforces safe subject IDs; keep this fixture path bounded too.
            directory = (attempt / "output/t1" / subject.subject_id).resolve()
            if not directory.is_relative_to(attempt.resolve()):
                return ExecutionResult(status="failed_terminal", error="invalid subject path")
            directory.mkdir(parents=True, exist_ok=True)
            header = bytearray(352 + 32)
            struct.pack_into("<i", header, 0, 348)
            struct.pack_into("<8h", header, 40, 3, 2, 2, 2, 1, 1, 1, 1)
            struct.pack_into("<2h", header, 70, 16, 32)
            struct.pack_into("<8f", header, 76, 1, 1, 1, 1, 0, 0, 0, 0)
            struct.pack_into("<f", header, 108, 352)
            struct.pack_into("<h", header, 254, 1)
            struct.pack_into("<12f", header, 280, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0)
            header[344:348] = b"n+1\0"
            image = directory / "coSYNTHETIC_NON_SCIENTIFIC.nii"
            image.write_bytes(header)
            preview = directory / "inspection.png"
            preview.write_bytes(
                base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6X8AAAAASUVORK5CYII="
                )
            )
            row = {
                "subject_id": subject.subject_id,
                "relative_path": image.relative_to(attempt).as_posix(),
                "inspection_image": preview.relative_to(attempt).as_posix(),
                "metadata": metadata,
            }
            outputs.append(row)
            for path, role in (
                (image, "preprocessing.t1_prepared"),
                (preview, "preprocessing.t1_inspection_image"),
            ):
                artifacts.append(
                    {
                        "artifact_type": role,
                        "relative_path": path.relative_to(
                            Path(project.work_root) / run.run_id
                        ).as_posix(),
                        "checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "size_bytes": path.stat().st_size,
                        "provenance": {
                            "executor": "workflow_mock",
                            "non_scientific": True,
                            "non_scientific_reason": "synthetic fixture; not processed anatomy",
                            "subject_id": subject.subject_id,
                            "manifest_hash": plan.manifest_hash,
                            "operation": "t1_preparation",
                            "metadata": metadata,
                        },
                    }
                )
        evidence = attempt / "output/preprocessing-evidence.json"
        evidence.write_text(
            json.dumps(
                {
                    "operation": "t1_preparation",
                    "subjects": [s.subject_id for s in manifest.subjects],
                    "inspection_only": True,
                    "outputs": outputs,
                    "non_scientific": True,
                }
            ),
            encoding="utf-8",
        )
        artifacts.append(
            {
                "artifact_type": "preprocessing.metadata_evidence",
                "relative_path": evidence.relative_to(
                    Path(project.work_root) / run.run_id
                ).as_posix(),
                "checksum": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                "size_bytes": evidence.stat().st_size,
                "provenance": {
                    "executor": "workflow_mock",
                    "non_scientific": True,
                    "operation": "t1_preparation",
                },
            }
        )
        return ExecutionResult(
            status="succeeded",
            output={
                "executor": "workflow_mock",
                "operation": "t1_preparation",
                "non_scientific": True,
            },
            artifacts=tuple(artifacts),
        )
