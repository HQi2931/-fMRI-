"""Durable preparation identities for one ready step in a linear WorkTask."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from neuroagent.application.automation.models import (
    AuthorityReferences,
    TaskStatus,
    TaskStep,
    TaskStepStatus,
    WorkTask,
)
from neuroagent.application.automation.step_lifecycle import resolve_linear_step_sequence

SUPPORTED_PREPARATION_STEP_KINDS = frozenset({"preprocessing_run", "statistical_run"})


class StepPreparationTarget(BaseModel):
    """Persisted identity of the unique linear successor that may be prepared."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1, max_length=100)
    task_version: int = Field(ge=1)
    step_id: str = Field(min_length=1, max_length=100)
    step_kind: str = Field(min_length=1, max_length=100)
    step_version: int = Field(ge=1)
    input_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class StepPreparationClaim(BaseModel):
    """Fenced lease for preparing one exact TaskStep version and input snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1, max_length=100)
    task_version: int = Field(ge=1)
    step_id: str = Field(min_length=1, max_length=100)
    step_kind: str = Field(min_length=1, max_length=100)
    step_version: int = Field(ge=1)
    input_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    worker_id: str = Field(min_length=1, max_length=200)
    claim_token: str = Field(min_length=32, max_length=64)
    lease_expires_at: float = Field(gt=0)


def resolve_step_preparation_target(task: WorkTask) -> StepPreparationTarget | None:
    """Resolve a prepare target only for a valid multi-step RUNNING successor.

    The sequence parser enforces a successful prefix, at most one active step,
    and a pending suffix. A single-step task remains on its existing lifecycle.
    Malformed sequences raise ``ValueError`` so callers can fail closed.
    """

    if task.status is not TaskStatus.RUNNING or len(task.steps) < 2:
        return None

    ordered = resolve_linear_step_sequence(task)
    ready_steps = tuple(step for step in ordered if step.status is TaskStepStatus.READY)
    if len(ready_steps) != 1:
        return None
    step = ready_steps[0]
    index = ordered.index(step)
    if (
        index == 0
        or step.kind not in SUPPORTED_PREPARATION_STEP_KINDS
        or any(item.status is not TaskStepStatus.SUCCEEDED for item in ordered[:index])
        or any(item.status is not TaskStepStatus.PENDING for item in ordered[index + 1 :])
    ):
        return None

    fingerprint = _step_input_fingerprint(task, ordered, index)
    return StepPreparationTarget(
        task_id=task.task_id,
        task_version=task.version,
        step_id=step.step_id,
        step_kind=step.kind,
        step_version=step.version,
        input_fingerprint=fingerprint,
    )


def _step_input_fingerprint(
    task: WorkTask,
    ordered: tuple[TaskStep, ...],
    target_index: int,
) -> str:
    target = ordered[target_index]
    evidence = {
        "schema": "worktask-step-preparation-v1",
        "task_id": task.task_id,
        "task_version": task.version,
        "goal": task.goal.model_dump(mode="json"),
        "task_authority_refs": _authority_refs(task.authority_refs),
        "budget": task.budget.model_dump(mode="json"),
        "target": _step_identity(target),
        "successful_predecessors": [_step_identity(step) for step in ordered[:target_index]],
    }
    encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _step_identity(step: TaskStep) -> dict[str, Any]:
    identity = step.model_dump(mode="json")
    identity["authority_refs"] = _authority_refs(step.authority_refs)
    return identity


def _authority_refs(refs: AuthorityReferences) -> dict[str, Any]:
    result = refs.model_dump(mode="json")
    result["artifact_ids"] = sorted(result["artifact_ids"])
    return result
