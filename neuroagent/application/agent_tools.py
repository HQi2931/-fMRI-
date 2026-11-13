"""Server-owned tool dispatch for the bounded Agent tool loop.

The dispatcher is the single place where a model-requested tool name becomes
server work.  Three properties matter:

* It is built only from model-visible tools, so a privileged (EXECUTE) tool has
  no entry to dispatch at all.
* It re-checks the tier at dispatch time, so even a forged catalog cannot reach
  privileged work.
* Every outcome is sanitised and audited before the model sees it.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import jsonschema

from neuroagent.agent.models import ToolCall
from neuroagent.agent.redaction import OutboundContextPolicy, OutboundPolicyError
from neuroagent.agent.tool_result import ToolCallOutcome, ToolCallStatus, ToolResultSanitizer
from neuroagent.agent.tool_spec import (
    LlmTool,
    ToolSpecError,
    build_model_visible_catalog,
    require_model_visible,
)
from neuroagent.application.errors import ApplicationError

logger = logging.getLogger(__name__)

_MAX_MESSAGE_CHARS = 500

ToolHandler = Callable[[Mapping[str, Any]], Any]
#: Receives the call, its outcome and the redacted arguments for the audit trail.
AuditSink = Callable[[ToolCall, ToolCallOutcome, dict[str, Any]], None]

_REJECTION_MESSAGES = {
    "unknown_tool": (
        "that tool is not available in this conversation; use only the tools you were offered"
    ),
    "tool_not_permitted": "that tool may not be called by a model",
    "invalid_arguments": "the arguments did not match the tool schema; retry with valid arguments",
}


@dataclass(frozen=True)
class ToolBinding:
    tool: LlmTool
    handler: ToolHandler


def _truncate_message(value: str) -> str:
    return value[:_MAX_MESSAGE_CHARS]


def _elapsed_ms(started: float, finished: float) -> int:
    return max(0, int((finished - started) * 1000))


def _validate_arguments(tool: LlmTool, arguments: Mapping[str, Any]) -> str | None:
    """Return a model-facing schema error message, or ``None`` when valid."""

    schema = tool.parameters
    if not schema:
        return None
    try:
        jsonschema.validate(
            instance=dict(arguments),
            schema=schema,
            cls=jsonschema.Draft202012Validator,
        )
    except jsonschema.ValidationError as exc:
        location = "/".join(str(part) for part in exc.absolute_path) or "<root>"
        return _truncate_message(f"arguments failed schema validation at {location}: {exc.message}")
    except jsonschema.SchemaError:
        logger.error("Tool %s declares an invalid parameter schema", tool.name)
        return "the tool parameter schema is invalid; no call was made"
    return None


class ToolDispatcher:
    def __init__(
        self,
        bindings: Iterable[ToolBinding],
        *,
        policy: OutboundContextPolicy,
        sanitizer: ToolResultSanitizer | None = None,
        audit: AuditSink | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        candidates = list(bindings)
        catalog = build_model_visible_catalog(binding.tool for binding in candidates)
        visible = {
            binding.tool.name: binding for binding in candidates if binding.tool.model_visible
        }
        self._bindings: dict[str, ToolBinding] = {tool.name: visible[tool.name] for tool in catalog}
        self._catalog = catalog
        self._policy = policy
        self._sanitizer = sanitizer or ToolResultSanitizer(policy)
        self._audit = audit
        self._clock = clock

    @property
    def catalog(self) -> tuple[LlmTool, ...]:
        """The tools a model may be offered: never includes EXECUTE tools."""

        return self._catalog

    def has(self, name: str) -> bool:
        return name in self._bindings

    async def dispatch(
        self, call: ToolCall, *, timeout_seconds: float | None = None
    ) -> ToolCallOutcome:
        started = self._clock()
        # Redact once: arguments are untrusted model output and they become part
        # of the persisted audit record.
        redacted = self.redacted_arguments(call)
        binding = self._bindings.get(call.name)
        if binding is None:
            return self._reject(call, "unknown_tool", started, redacted)

        # Defence in depth: unreachable through ``catalog``, but a forged or
        # hand-built dispatcher must still refuse privileged work loudly.
        try:
            require_model_visible(binding.tool)
        except ToolSpecError:
            logger.error("Refused dispatch of non model-visible tool %s", call.name)
            return self._reject(call, "tool_not_permitted", started, redacted)

        if not call.arguments_valid:
            return self._reject(call, "invalid_arguments", started, redacted)

        schema_error = _validate_arguments(binding.tool, call.arguments)
        if schema_error is not None:
            return self._outcome(
                call,
                binding.tool,
                ToolCallStatus.REJECTED,
                started,
                arguments=redacted,
                error_code="invalid_arguments",
                message=schema_error,
            )

        effective_timeout = (
            float(binding.tool.timeout_seconds)
            if timeout_seconds is None
            else min(float(binding.tool.timeout_seconds), max(0.001, timeout_seconds))
        )
        try:
            produced = await _run_handler(binding.handler, call.arguments, effective_timeout)
        except TimeoutError:
            return self._outcome(
                call,
                binding.tool,
                ToolCallStatus.FAILED,
                started,
                arguments=redacted,
                error_code="tool_timeout",
                message="the tool exceeded its bounded execution time",
            )
        except ApplicationError as exc:
            return self._outcome(
                call,
                binding.tool,
                ToolCallStatus.FAILED,
                started,
                arguments=redacted,
                error_code="application_error",
                message=_truncate_message(exc.message),
            )
        except Exception:
            logger.exception("Tool %s failed while executing", call.name)
            return self._outcome(
                call,
                binding.tool,
                ToolCallStatus.FAILED,
                started,
                arguments=redacted,
                error_code="tool_execution_failed",
                message="the tool failed to complete; retry or narrow the request",
            )

        if not isinstance(produced, Mapping):
            return self._outcome(
                call,
                binding.tool,
                ToolCallStatus.FAILED,
                started,
                arguments=redacted,
                error_code="tool_contract_violation",
                message="the tool did not return a structured result",
            )

        try:
            payload, redactions, truncated = self._sanitizer.sanitize(
                produced, max_bytes=binding.tool.max_result_bytes
            )
        except OutboundPolicyError:
            logger.warning("Tool %s returned a result that failed the outbound policy", call.name)
            return self._outcome(
                call,
                binding.tool,
                ToolCallStatus.FAILED,
                started,
                arguments=redacted,
                error_code="result_policy_violation",
                message="the tool result could not be proven safe to forward",
            )

        return self._outcome(
            call,
            binding.tool,
            ToolCallStatus.SUCCEEDED,
            started,
            arguments=redacted,
            result=payload,
            truncated=truncated,
            redactions=redactions,
        )

    def redacted_arguments(self, call: ToolCall) -> dict[str, Any]:
        """Redact raw arguments for the audit trail without affecting dispatch."""

        try:
            return dict(self._policy.redact_tool_result(call.arguments).payload)
        except OutboundPolicyError:
            return {}

    def _reject(
        self,
        call: ToolCall,
        code: str,
        started: float,
        arguments: dict[str, Any] | None = None,
    ) -> ToolCallOutcome:
        binding = self._bindings.get(call.name)
        outcome = ToolCallOutcome(
            call_id=call.call_id,
            name=call.name,
            # An unknown tool genuinely has no tier; do not invent one.
            tier=binding.tool.tier if binding is not None else None,
            status=ToolCallStatus.REJECTED,
            arguments=arguments or {},
            error_code=code,
            message=_REJECTION_MESSAGES[code],
            duration_ms=_elapsed_ms(started, self._clock()),
        )
        self._record(call, outcome, arguments or {})
        return outcome

    def _outcome(
        self,
        call: ToolCall,
        tool: LlmTool,
        status: ToolCallStatus,
        started: float,
        *,
        result: dict[str, Any] | None = None,
        arguments: dict[str, Any] | None = None,
        error_code: str | None = None,
        message: str | None = None,
        truncated: bool = False,
        redactions: int = 0,
    ) -> ToolCallOutcome:
        outcome = ToolCallOutcome(
            call_id=call.call_id,
            name=call.name,
            tier=tool.tier,
            status=status,
            result=result or {},
            arguments=arguments or {},
            error_code=error_code,
            message=message,
            duration_ms=_elapsed_ms(started, self._clock()),
            truncated=truncated,
            redactions=redactions,
        )
        self._record(call, outcome, arguments or {})
        return outcome

    def _record(
        self,
        call: ToolCall,
        outcome: ToolCallOutcome,
        arguments: dict[str, Any],
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit(call, outcome, arguments)
        except Exception:
            # Auditing must never turn a completed tool call into a failure.
            logger.exception("Tool audit sink failed for %s", call.name)


async def _run_handler(
    handler: ToolHandler,
    arguments: Mapping[str, Any],
    remaining_seconds: float | None,
) -> Any:
    """Run sync read tools off-loop and enforce the declared deadline."""

    timeout = None if remaining_seconds is None else max(0.001, remaining_seconds)
    if inspect.iscoroutinefunction(handler):
        produced = handler(arguments)
        return await asyncio.wait_for(produced, timeout=timeout)
    produced = await asyncio.wait_for(asyncio.to_thread(handler, arguments), timeout=timeout)
    if inspect.isawaitable(produced):
        return await asyncio.wait_for(produced, timeout=timeout)
    return produced
