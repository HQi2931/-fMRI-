"""Capability-to-tool binding with an explicit freeze boundary."""

from __future__ import annotations

from neuroagent.skills.models import ResolvedToolRef
from neuroagent.tools.models import ToolDefinition, ToolRisk


class ToolRegistryError(ValueError):
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._by_capability: dict[str, ToolDefinition] = {}
        self._frozen = False

    def register(self, definition: ToolDefinition) -> None:
        if self._frozen:
            raise ToolRegistryError("Tool registry is frozen")
        if definition.capability in self._by_capability:
            raise ToolRegistryError(f"duplicate capability: {definition.capability}")
        self._by_capability[definition.capability] = definition

    def freeze(self) -> None:
        self._frozen = True

    def resolve_capability(self, capability: str) -> ResolvedToolRef:
        try:
            return self._by_capability[capability].as_resolved_ref()
        except KeyError as exc:
            raise ToolRegistryError(f"no registered Tool provides {capability}") from exc


def build_default_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    definitions = (
        ("dataset.classify-input", "fmri.dataset.inspect", ToolRisk.READ_ONLY, 300),
        ("manifest.verify", "fmri.manifest.verify", ToolRisk.READ_ONLY, 300),
        (
            "artifact.verify-metadata",
            "fmri.artifact.verify_metadata",
            ToolRisk.READ_ONLY,
            3600,
        ),
        ("dpabi.stage-input", "fmri.dpabi.stage_input", ToolRisk.WORKSPACE_WRITE, 3600),
        (
            "dpabi.plan-bids-conversion",
            "fmri.dpabi.plan_bids_conversion",
            ToolRisk.READ_ONLY,
            300,
        ),
        ("dpabi.preprocess", "fmri.dpabi.preprocess", ToolRisk.COMPUTE, 86400),
        ("dpabi.alff-falff", "fmri.dpabi.alff_falff", ToolRisk.COMPUTE, 86400),
        ("dpabi.prepare-reho", "fmri.dpabi.prepare_reho", ToolRisk.COMPUTE, 86400),
        ("dpabi.reho", "fmri.dpabi.reho", ToolRisk.COMPUTE, 86400),
        ("qc.metrics", "fmri.qc.metrics", ToolRisk.READ_ONLY, 3600),
        ("qc.pre-statistics", "fmri.qc.pre_statistics", ToolRisk.READ_ONLY, 3600),
        ("dpabi.statistics-ttest", "fmri.dpabi.statistics.ttest", ToolRisk.COMPUTE, 43200),
        ("dpabi.statistics-fdr", "fmri.dpabi.statistics.fdr", ToolRisk.COMPUTE, 43200),
        ("dpabi.statistics-grf", "fmri.dpabi.statistics.grf", ToolRisk.COMPUTE, 43200),
    )
    for tool_id, capability, risk, timeout in definitions:
        registry.register(
            ToolDefinition(
                tool_id=tool_id,
                version="1.0.0",
                capability=capability,
                risk=risk,
                supports_dry_run=True,
                timeout_seconds=timeout,
                input_schema_ref=f"schemas/{tool_id}.input.json",
                output_schema_ref=f"schemas/{tool_id}.output.json",
            )
        )
    registry.freeze()
    return registry
