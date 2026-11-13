"""Truthful, versioned directory of Work statistical execution capabilities.

The installed DPABI can contain a function before this application has a safe
design adapter and a complete result contract for it.  ``executable`` therefore
tracks the application path, while ``dpabi_function`` records the local entry
point the adapter is expected to call.
"""

from __future__ import annotations

from typing import TypedDict


class StatisticalCapability(TypedDict):
    test: str
    label: str
    dpabi_function: str
    design_requirements: tuple[str, ...]
    result_contract: tuple[str, ...]
    executable: bool
    validation_status: str


STATISTICAL_CAPABILITIES: tuple[StatisticalCapability, ...] = (
    {
        "test": "one_sample_t",
        "label": "单样本 t 检验",
        "dpabi_function": "y_TTest1_Image",
        "design_requirements": ("metric", "mask", "baseline", "tail"),
        "result_contract": ("T map", "effect map", "design matrix", "cluster table"),
        "executable": True,
        "validation_status": "real_matlab_smoke_passed_dpabi_v9_0_250415",
    },
    {
        "test": "independent_two_sample_t",
        "label": "两独立样本 t 检验",
        "dpabi_function": "y_TTest2_Image",
        "design_requirements": ("metric", "mask", "two ordered groups", "tail"),
        "result_contract": ("T map", "effect map", "design matrix", "cluster table"),
        "executable": True,
        "validation_status": "real_matlab_smoke_passed_dpabi_v9_0_250415",
    },
    {
        "test": "paired_t",
        "label": "配对 t 检验",
        "dpabi_function": "y_TTestPaired_Image",
        "design_requirements": ("metric", "mask", "two ordered conditions", "subject pairing"),
        "result_contract": ("T map", "effect map", "design matrix", "cluster table"),
        "executable": True,
        "validation_status": "real_matlab_smoke_passed_dpabi_v9_0_250415",
    },
    {
        "test": "correlation",
        "label": "相关/偏相关",
        "dpabi_function": "y_Correlation_Image",
        "design_requirements": ("metric", "mask", "registered numeric predictor", "tail"),
        "result_contract": ("R map", "effect map", "design matrix", "cluster table"),
        "executable": True,
        "validation_status": "real_matlab_smoke_passed_dpabi_v9_0_250415",
    },
    {
        "test": "regression",
        "label": "多变量回归",
        "dpabi_function": "y_GroupAnalysis_Image",
        "design_requirements": ("metric", "mask", "registered predictors", "contrast"),
        "result_contract": ("T map", "effect map", "design matrix", "cluster table"),
        "executable": True,
        "validation_status": "real_matlab_smoke_passed_dpabi_v9_0_250415",
    },
    {
        "test": "one_way_anova_ancova",
        "label": "单因素 ANOVA/ANCOVA",
        "dpabi_function": "y_ANCOVA1_Image",
        "design_requirements": ("metric", "mask", "group factor", "registered covariates"),
        "result_contract": ("F map", "effect map", "design matrix", "cluster table"),
        "executable": True,
        "validation_status": "real_matlab_smoke_passed_dpabi_v9_0_250415",
    },
)


def model_visible_capabilities() -> list[dict[str, object]]:
    """Return JSON-safe entries; unsupported methods remain clearly gated."""

    return [
        {
            "test": item["test"],
            "label": item["label"],
            "dpabi_function": item["dpabi_function"],
            "design_requirements": list(item["design_requirements"]),
            "result_contract": list(item["result_contract"]),
            "executable": item["executable"],
            "validation_status": item["validation_status"],
        }
        for item in STATISTICAL_CAPABILITIES
    ]
