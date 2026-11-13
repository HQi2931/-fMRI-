"""The tier boundary is the core safety property of the Agent tool loop."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from neuroagent.agent.tool_spec import (
    MODEL_VISIBLE_TIERS,
    LlmTool,
    ToolSpecError,
    ToolTier,
    build_model_visible_catalog,
    provider_tool_payload,
    require_model_visible,
)


def _tool(name: str, tier: ToolTier) -> LlmTool:
    return LlmTool(name=name, description=f"{name} tool", tier=tier)


def test_execute_tools_never_reach_a_model_visible_catalog() -> None:
    catalog = build_model_visible_catalog(
        [
            _tool("get_run_status", ToolTier.READ),
            _tool("prepare_preprocessing_plan", ToolTier.PROPOSE),
            _tool("start_preprocessing", ToolTier.EXECUTE),
            _tool("approve_qc_review", ToolTier.EXECUTE),
            _tool("skip_qc", ToolTier.EXECUTE),
        ]
    )

    names = [tool.name for tool in catalog]
    assert names == ["get_run_status", "prepare_preprocessing_plan"]
    assert all(tool.tier in MODEL_VISIBLE_TIERS for tool in catalog)


def test_provider_payload_omits_privileged_tools() -> None:
    payload = provider_tool_payload(
        build_model_visible_catalog(
            [_tool("get_run_status", ToolTier.READ), _tool("start_preprocessing", ToolTier.EXECUTE)]
        )
    )

    assert [entry["function"]["name"] for entry in payload] == ["get_run_status"]
    assert all(entry["type"] == "function" for entry in payload)
    assert payload[0]["function"]["parameters"] == {"type": "object", "properties": {}}


def test_catalog_is_sorted_and_rejects_duplicate_names() -> None:
    catalog = build_model_visible_catalog(
        [_tool("zzz_read", ToolTier.READ), _tool("aaa_read", ToolTier.READ)]
    )
    assert [tool.name for tool in catalog] == ["aaa_read", "zzz_read"]

    with pytest.raises(ToolSpecError, match="duplicate tool name"):
        build_model_visible_catalog(
            [_tool("same_name", ToolTier.READ), _tool("same_name", ToolTier.PROPOSE)]
        )


def test_require_model_visible_refuses_privileged_tools() -> None:
    require_model_visible(_tool("get_run_status", ToolTier.READ))

    with pytest.raises(ToolSpecError, match="not model-visible"):
        require_model_visible(_tool("start_preprocessing", ToolTier.EXECUTE))


@pytest.mark.parametrize(
    "name",
    ["with.dot", "with space", "", "a" * 65, "semi;colon", "中文名"],
)
def test_tool_names_follow_the_provider_function_constraint(name: str) -> None:
    with pytest.raises(ValidationError):
        _tool(name, ToolTier.READ)


def test_tool_parameters_must_be_an_object_schema() -> None:
    with pytest.raises(ValidationError, match="object JSON Schema"):
        LlmTool(
            name="bad_schema",
            description="array schema is not a function contract",
            parameters={"type": "array"},
            tier=ToolTier.READ,
        )


def test_tool_result_budget_defaults_are_bounded() -> None:
    tool = _tool("get_run_status", ToolTier.READ)
    assert 0 < tool.max_result_bytes <= 1_048_576
    assert 0 < tool.timeout_seconds <= 3_600
