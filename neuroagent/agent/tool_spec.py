"""Model-facing tool contracts with an explicit risk-tier boundary.

The tier boundary is the safety property of the whole Agent tool loop:

* ``READ`` tools only observe state and may be called freely.
* ``PROPOSE`` tools may write drafts, revisions and proposals, but never start
  computation.
* ``EXECUTE`` tools start or approve real work.  They are **never** advertised
  to a model, never described in a system prompt and never serialized into a
  provider request.  They stay reachable only from server-owned code paths that
  run after an explicit human confirmation.

Filtering EXECUTE tools while building the catalog (instead of refusing them at
dispatch time) means a privileged tool cannot leak into a prompt by accident,
and a model that guesses a privileged name still finds nothing to call.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# OpenAI-compatible function names allow letters, digits, underscore and dash.
_TOOL_NAME_PATTERN = r"^[a-zA-Z0-9_-]{1,64}$"
_TOOL_NAME = re.compile(_TOOL_NAME_PATTERN)

DEFAULT_TOOL_TIMEOUT_SECONDS = 30
DEFAULT_TOOL_RESULT_MAX_BYTES = 65_536


class ToolTier(StrEnum):
    READ = "read"
    PROPOSE = "propose"
    EXECUTE = "execute"


#: Tiers that may ever be shown to a model.
MODEL_VISIBLE_TIERS = frozenset({ToolTier.READ, ToolTier.PROPOSE})


class ToolSpecError(ValueError):
    pass


class LlmTool(BaseModel):
    """One tool as the model sees it: name, prose contract and JSON Schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=_TOOL_NAME_PATTERN)
    description: str = Field(min_length=1, max_length=2_000)
    parameters: dict[str, Any] = Field(default_factory=dict)
    tier: ToolTier
    timeout_seconds: int = Field(default=DEFAULT_TOOL_TIMEOUT_SECONDS, gt=0, le=3_600)
    max_result_bytes: int = Field(default=DEFAULT_TOOL_RESULT_MAX_BYTES, gt=0)

    @field_validator("parameters")
    @classmethod
    def object_schema_only(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value and value.get("type") != "object":
            raise ValueError("tool parameters must be an object JSON Schema")
        return value

    @property
    def model_visible(self) -> bool:
        return self.tier in MODEL_VISIBLE_TIERS

    def to_provider_tool(self) -> dict[str, Any]:
        """Render the OpenAI-compatible function-tool payload."""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }


def build_model_visible_catalog(tools: Iterable[LlmTool]) -> tuple[LlmTool, ...]:
    """Return the tools a model may see, ordered and duplicate-free.

    EXECUTE tools are dropped here, so they can never be serialized into a
    request or prompt.  Duplicate names are a programming error rather than a
    silent overwrite, because an ambiguous catalog would make dispatch
    unpredictable.
    """

    catalog: dict[str, LlmTool] = {}
    for tool in tools:
        if not tool.model_visible:
            continue
        if tool.name in catalog:
            raise ToolSpecError(f"duplicate tool name: {tool.name}")
        catalog[tool.name] = tool
    return tuple(catalog[name] for name in sorted(catalog))


def require_model_visible(tool: LlmTool) -> None:
    """Defence in depth for the dispatcher: refuse privileged tools loudly."""

    if not tool.model_visible:
        raise ToolSpecError(f"tool {tool.name} is not model-visible (tier {tool.tier.value})")


def provider_tool_payload(tools: Sequence[LlmTool]) -> list[dict[str, Any]]:
    """Serialize a catalog for an OpenAI-compatible ``tools`` field."""

    return [tool.to_provider_tool() for tool in tools]
