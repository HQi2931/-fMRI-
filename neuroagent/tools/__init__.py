"""Registered deterministic Tool definitions and DPABI adapters."""

from neuroagent.tools.dpabi_v82 import (
    CorrectionCall,
    DpabiCfgProjection,
    DpabiMetricRequest,
    DpabiPreprocessingRequest,
    DpabiV82Adapter,
    StatisticsCall,
)
from neuroagent.tools.input_staging import (
    DpabiBidsConverterCfgProjection,
    DpabiBidsConverterJobPlan,
    DpabiBidsConverterPlanTool,
    InputClassification,
    InputFormat,
    ManifestFile,
    ReadOnlyInputClassifier,
    StagingCopyOperation,
    StagingCopyPlan,
    StagingCopyResult,
    StagingCopyTool,
)
from neuroagent.tools.models import ToolDefinition, ToolRisk
from neuroagent.tools.registry import ToolRegistry, ToolRegistryError, build_default_tool_registry

__all__ = [
    "CorrectionCall",
    "DpabiBidsConverterCfgProjection",
    "DpabiBidsConverterJobPlan",
    "DpabiBidsConverterPlanTool",
    "DpabiCfgProjection",
    "DpabiMetricRequest",
    "DpabiPreprocessingRequest",
    "DpabiV82Adapter",
    "InputClassification",
    "InputFormat",
    "ManifestFile",
    "ReadOnlyInputClassifier",
    "StagingCopyOperation",
    "StagingCopyPlan",
    "StagingCopyResult",
    "StagingCopyTool",
    "StatisticsCall",
    "ToolDefinition",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolRisk",
    "build_default_tool_registry",
]
