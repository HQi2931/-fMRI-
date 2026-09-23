"""Tool-call outcomes plus the sanitisation boundary for model context."""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from neuroagent.agent.redaction import OutboundContextPolicy
from neuroagent.agent.tool_spec import ToolTier

#: Progressive per-leaf budgets used when a result exceeds its size budget.
_SHRINK_LIMITS = (4_000, 1_000, 256)


class ToolCallStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


class ToolCallOutcome(BaseModel):
    """The audited result of one model-requested tool invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str
    name: str
    tier: ToolTier | None = None
    status: ToolCallStatus
    result: dict[str, Any] = Field(default_factory=dict)
    #: Redacted arguments, so one outcome is a complete audit record.
    arguments: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    message: str | None = None
    duration_ms: int = Field(default=0, ge=0)
    truncated: bool = False
    redactions: int = Field(default=0, ge=0)

    @property
    def succeeded(self) -> bool:
        return self.status is ToolCallStatus.SUCCEEDED


class ToolResultSanitizer:
    """Redact tool output, then bound it to a byte budget.

    Redaction reuses :meth:`OutboundContextPolicy.redact_tool_result`, so tool
    results cannot carry a path, e-mail, phone number or clinical narrative into
    the model context.  Shrinking happens afterwards and only truncates string
    leaves, preserving the structure the model needs to reason about.
    """

    def __init__(self, policy: OutboundContextPolicy) -> None:
        self._policy = policy

    def sanitize(
        self, payload: Mapping[str, Any], *, max_bytes: int
    ) -> tuple[dict[str, Any], int, bool]:
        """Return ``(payload, redaction_count, truncated)``.

        Raises :class:`OutboundPolicyError` for structural violations, which
        indicate a defective tool contract rather than hostile input.
        """

        redacted = self._policy.redact_tool_result(payload)
        sanitized = dict(redacted.payload)
        if _encoded_size(sanitized) <= max_bytes:
            return sanitized, redacted.redaction_count, False

        for limit in _SHRINK_LIMITS:
            shrunk = _shrink_strings(sanitized, limit)
            if isinstance(shrunk, dict) and _encoded_size(shrunk) <= max_bytes:
                return shrunk, redacted.redaction_count, True

        envelope = {
            "truncated": True,
            "original_bytes": _encoded_size(sanitized),
            "note": (
                "Tool result exceeded its size budget and was withheld. "
                "Retry with a narrower query or ask for a smaller range."
            ),
        }
        return envelope, redacted.redaction_count, True


def _encoded_size(payload: object) -> int:
    return len(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _shrink_strings(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, dict):
        return {key: _shrink_strings(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_shrink_strings(item, limit) for item in value]
    return value
