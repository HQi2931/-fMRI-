"""Dispatcher tier enforcement, argument validation, redaction and audit."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from neuroagent.agent.models import ToolCall
from neuroagent.agent.redaction import OutboundContextPolicy
from neuroagent.agent.tool_result import ToolCallStatus
from neuroagent.agent.tool_spec import LlmTool, ToolTier
from neuroagent.application.agent_tools import ToolBinding, ToolDispatcher
from neuroagent.application.errors import InputValidationError

_SALT = "dispatcher-test-salt-value"

_STATUS_PARAMETERS = {
    "type": "object",
    "properties": {"run_id": {"type": "string"}},
    "required": ["run_id"],
    "additionalProperties": False,
}


def _read_tool() -> LlmTool:
    return LlmTool(
        name="get_run_status",
        description="Read run state.",
        parameters=_STATUS_PARAMETERS,
        tier=ToolTier.READ,
    )


def _propose_tool() -> LlmTool:
    return LlmTool(
        name="prepare_preprocessing_plan",
        description="Draft a preprocessing plan.",
        parameters={"type": "object", "properties": {}},
        tier=ToolTier.PROPOSE,
    )


def _execute_tool() -> LlmTool:
    return LlmTool(
        name="start_preprocessing",
        description="Queue real work.",
        parameters={"type": "object", "properties": {}},
        tier=ToolTier.EXECUTE,
    )


def _dispatcher(
    *,
    read: Any = None,
    propose: Any = None,
    execute: Any = None,
    audit: Any = None,
) -> ToolDispatcher:
    bindings = [_read_tool(), _propose_tool(), _execute_tool()]
    handlers = {
        "get_run_status": read or (lambda arguments: {"state": "running"}),
        "prepare_preprocessing_plan": propose or (lambda arguments: {"plan": "draft"}),
        "start_preprocessing": execute or (lambda arguments: {"started": True}),
    }
    return ToolDispatcher(
        [ToolBinding(tool=tool, handler=handlers[tool.name]) for tool in bindings],
        policy=OutboundContextPolicy(_SALT),
        audit=audit,
    )


def _call(name: str, arguments: dict[str, Any] | None = None, **kwargs: Any) -> ToolCall:
    return ToolCall(call_id="call-1", name=name, arguments=arguments or {}, **kwargs)


def test_catalog_never_contains_execute_tools() -> None:
    dispatcher = _dispatcher()
    assert [tool.name for tool in dispatcher.catalog] == [
        "get_run_status",
        "prepare_preprocessing_plan",
    ]
    assert dispatcher.has("start_preprocessing") is False


@pytest.mark.asyncio
async def test_execute_tool_cannot_be_dispatched_even_when_named() -> None:
    called: list[str] = []

    def forbidden(arguments: dict[str, Any]) -> dict[str, Any]:
        called.append("start_preprocessing")
        return {"started": True}

    dispatcher = _dispatcher(execute=forbidden)
    outcome = await dispatcher.dispatch(_call("start_preprocessing"))

    assert outcome.status is ToolCallStatus.REJECTED
    assert outcome.error_code == "unknown_tool"
    assert outcome.tier is None
    assert called == []


@pytest.mark.asyncio
async def test_unknown_tool_is_rejected_without_running_anything() -> None:
    outcome = await _dispatcher().dispatch(_call("delete_everything"))

    assert outcome.status is ToolCallStatus.REJECTED
    assert outcome.error_code == "unknown_tool"
    assert outcome.result == {}


@pytest.mark.asyncio
async def test_valid_call_succeeds_and_reports_duration() -> None:
    outcome = await _dispatcher().dispatch(_call("get_run_status", {"run_id": "r1"}))

    assert outcome.status is ToolCallStatus.SUCCEEDED
    assert outcome.result == {"state": "running"}
    assert outcome.tier is ToolTier.READ
    assert outcome.duration_ms >= 0


@pytest.mark.asyncio
async def test_schema_violation_is_rejected_with_a_repair_hint() -> None:
    outcome = await _dispatcher().dispatch(_call("get_run_status", {"wrong": 1}))

    assert outcome.status is ToolCallStatus.REJECTED
    assert outcome.error_code == "invalid_arguments"
    assert outcome.message is not None
    assert "run_id" in outcome.message


@pytest.mark.asyncio
async def test_malformed_provider_arguments_are_rejected_before_dispatch() -> None:
    outcome = await _dispatcher().dispatch(_call("get_run_status", {}, arguments_valid=False))

    assert outcome.status is ToolCallStatus.REJECTED
    assert outcome.error_code == "invalid_arguments"


@pytest.mark.asyncio
async def test_application_error_becomes_a_failed_outcome_not_an_exception() -> None:
    def boom(arguments: dict[str, Any]) -> dict[str, Any]:
        raise InputValidationError("run_not_found", "运行不存在。")

    outcome = await _dispatcher(read=boom).dispatch(_call("get_run_status", {"run_id": "x"}))

    assert outcome.status is ToolCallStatus.FAILED
    assert outcome.error_code == "application_error"
    assert outcome.message == "运行不存在。"


@pytest.mark.asyncio
async def test_unexpected_tool_error_is_contained() -> None:
    def boom(arguments: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(r"D:\private\internal\path.txt")

    outcome = await _dispatcher(read=boom).dispatch(_call("get_run_status", {"run_id": "x"}))

    assert outcome.status is ToolCallStatus.FAILED
    assert outcome.error_code == "tool_execution_failed"
    assert outcome.message is not None
    assert "private" not in outcome.message


@pytest.mark.asyncio
async def test_non_mapping_tool_result_is_a_contract_violation() -> None:
    outcome = await _dispatcher(read=lambda arguments: "just a string").dispatch(
        _call("get_run_status", {"run_id": "x"})
    )

    assert outcome.status is ToolCallStatus.FAILED
    assert outcome.error_code == "tool_contract_violation"


@pytest.mark.asyncio
async def test_tool_result_paths_are_redacted_before_the_model_sees_them() -> None:
    def leaky(arguments: dict[str, Any]) -> dict[str, Any]:
        return {"log": r"wrote output to D:\private research\runs\out.nii"}

    outcome = await _dispatcher(read=leaky).dispatch(_call("get_run_status", {"run_id": "x"}))

    assert outcome.status is ToolCallStatus.SUCCEEDED
    assert "[redacted-path]" in outcome.result["log"]
    assert "private research" not in outcome.result["log"]
    assert outcome.redactions >= 1


@pytest.mark.asyncio
async def test_clinical_prose_is_allowed_but_structured_identity_is_dropped() -> None:
    def clinical(arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": "running",
            "note": "患者脑功能影像研究",
            "patient_name": "张三",
            "count": 3,
        }

    outcome = await _dispatcher(read=clinical).dispatch(_call("get_run_status", {"run_id": "x"}))

    assert outcome.status is ToolCallStatus.SUCCEEDED
    assert outcome.result["note"] == "患者脑功能影像研究"
    assert "patient_name" not in outcome.result
    assert outcome.result["state"] == "running"
    assert outcome.result["count"] == 3


@pytest.mark.asyncio
async def test_binary_tool_result_fails_closed() -> None:
    outcome = await _dispatcher(read=lambda arguments: {"blob": b"\x00\x01"}).dispatch(
        _call("get_run_status", {"run_id": "x"})
    )

    assert outcome.status is ToolCallStatus.FAILED
    assert outcome.error_code == "result_policy_violation"


@pytest.mark.asyncio
async def test_oversized_tool_result_is_bounded_and_flagged() -> None:
    def verbose(arguments: dict[str, Any]) -> dict[str, Any]:
        # 50 leaves is within the structural width bound; total size is not.
        return {"lines": ["x" * 5_000 for _ in range(50)], "total": 50}

    outcome = await _dispatcher(read=verbose).dispatch(_call("get_run_status", {"run_id": "x"}))

    assert outcome.status is ToolCallStatus.SUCCEEDED
    assert outcome.truncated is True
    assert outcome.result["total"] == 50
    assert len(str(outcome.result)) < 70_000


@pytest.mark.asyncio
async def test_structurally_wide_tool_result_fails_closed() -> None:
    """Tools must bound their own output; widening it is a contract defect."""

    def too_many(arguments: dict[str, Any]) -> dict[str, Any]:
        return {"artifacts": [{"name": f"a{index}"} for index in range(150)]}

    outcome = await _dispatcher(read=too_many).dispatch(_call("get_run_status", {"run_id": "x"}))

    assert outcome.status is ToolCallStatus.FAILED
    assert outcome.error_code == "result_policy_violation"


@pytest.mark.asyncio
async def test_audit_sink_receives_redacted_arguments() -> None:
    recorded: list[tuple[ToolCall, str, dict[str, Any]]] = []

    def audit(call: ToolCall, outcome: Any, arguments: dict[str, Any]) -> None:
        recorded.append((call, outcome.status.value, arguments))

    dispatcher = _dispatcher(audit=audit)
    await dispatcher.dispatch(_call("get_run_status", {"run_id": "subject-07"}))

    assert len(recorded) == 1
    _, status, arguments = recorded[0]
    assert status == "succeeded"
    # Subject identifiers are pseudonymised before they reach the audit trail.
    assert "subject-07" not in arguments["run_id"]


@pytest.mark.asyncio
async def test_audit_failure_never_breaks_a_successful_tool_call() -> None:
    def audit(call: ToolCall, outcome: Any, arguments: dict[str, Any]) -> None:
        raise RuntimeError("audit store down")

    outcome = await _dispatcher(audit=audit).dispatch(_call("get_run_status", {"run_id": "r1"}))

    assert outcome.status is ToolCallStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_async_tool_handlers_are_awaited() -> None:
    async def slow(arguments: dict[str, Any]) -> dict[str, Any]:
        return {"state": "async-ok"}

    outcome = await _dispatcher(read=slow).dispatch(_call("get_run_status", {"run_id": "r1"}))

    assert outcome.result == {"state": "async-ok"}


@pytest.mark.asyncio
async def test_tool_deadline_is_enforced() -> None:
    async def slow(arguments: dict[str, Any]) -> dict[str, Any]:
        del arguments
        await asyncio.sleep(0.05)
        return {"state": "too-late"}

    outcome = await _dispatcher(read=slow).dispatch(
        _call("get_run_status", {"run_id": "r1"}), timeout_seconds=0.01
    )

    assert outcome.status is ToolCallStatus.FAILED
    assert outcome.error_code == "tool_timeout"


def test_duplicate_tool_names_are_a_programming_error() -> None:
    from neuroagent.agent.tool_spec import ToolSpecError

    duplicate = LlmTool(
        name="get_run_status",
        description="same name, different intent",
        tier=ToolTier.READ,
    )
    with pytest.raises(ToolSpecError, match="duplicate tool name"):
        ToolDispatcher(
            [
                ToolBinding(tool=_read_tool(), handler=lambda arguments: {}),
                ToolBinding(tool=duplicate, handler=lambda arguments: {}),
            ],
            policy=OutboundContextPolicy(_SALT),
        )
