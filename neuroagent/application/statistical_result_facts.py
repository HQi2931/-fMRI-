"""Verified, bounded statistical method facts and descriptive ROI aggregates."""

# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping
from statistics import mean, stdev
from typing import Any

from neuroagent.application.contracts import (
    StatisticalClusterRoiView,
    StatisticalMethodSummary,
    StatisticalResultDetailView,
)
from neuroagent.application.hashing import content_hash
from neuroagent.domain.fmri.results import StatisticalResultManifest
from neuroagent.domain.fmri.statistics import (
    GrfCorrection,
    StatisticalDesignRevision,
    StatisticalTest,
    residual_degrees_of_freedom,
)

_METHODS = {
    StatisticalTest.ONE_SAMPLE_T: ("单样本 t 检验", "y_TTest1_Image", "T"),
    StatisticalTest.INDEPENDENT_TWO_SAMPLE_T: ("两样本 t 检验", "y_TTest2_Image", "T"),
    StatisticalTest.PAIRED_T: ("配对 t 检验", "y_TTestPaired_Image", "T"),
    StatisticalTest.CORRELATION: ("相关／偏相关", "y_Correlation_Image", "R"),
    StatisticalTest.REGRESSION: ("多变量回归", "y_GroupAnalysis_Image", "T"),
    StatisticalTest.ONE_WAY_ANOVA_ANCOVA: (
        "单因素 ANOVA／ANCOVA 总体 F 检验",
        "y_ANCOVA1_Image",
        "F",
    ),
}


def verified_result_design(
    detail: StatisticalResultDetailView,
) -> tuple[StatisticalResultManifest, StatisticalDesignRevision, dict[str, Any]]:
    """Restore the immutable design without trusting a report-shaped dictionary."""
    digest = hashlib.sha256(f"{detail.report_json}\n{detail.report_markdown}".encode()).hexdigest()
    if digest != detail.bundle_hash:
        raise ValueError("statistical report bundle hash mismatch")
    manifest = StatisticalResultManifest.model_validate(detail.manifest)
    report = json.loads(detail.report_json)
    design_payload = report["statistical_design"]
    if content_hash(design_payload) != report["frozen_bindings"]["design_hash"]:
        raise ValueError("statistical design hash mismatch")
    design = StatisticalDesignRevision.model_validate(design_payload)
    result = report["result"]
    if (
        (manifest.result_id, manifest.run_id, manifest.design_revision_id, manifest.mode.value)
        != (detail.result_id, detail.run_id, detail.design_revision_id, detail.mode)
        or design.revision_id != manifest.design_revision_id
        or result["result_id"] != manifest.result_id
        or result["mode"] != manifest.mode.value
        or manifest.non_scientific != detail.non_scientific
        or report["correction"]
        != (manifest.correction.model_dump(mode="json") if manifest.correction else None)
        or sorted(report["clusters"], key=lambda item: item["cluster_id"])
        != [
            item.model_dump(mode="json")
            for item in sorted(manifest.clusters, key=lambda item: item.cluster_id)
        ]
        or report["artifact_manifest_hash"]
        != content_hash(
            [
                item.model_dump(mode="json")
                for item in sorted(
                    manifest.artifacts, key=lambda item: (item.role.value, item.artifact_id)
                )
            ]
        )
    ):
        raise ValueError("statistical report and result manifest disagree")
    return manifest, design, report


def build_method_summary(
    detail: StatisticalResultDetailView,
    *,
    correction_evidence: Mapping[str, Any] | None = None,
    software_evidence: Mapping[str, Any] | None = None,
    correction_evidence_status: str = "not_registered",
    evidence_issues: tuple[str, ...] = (),
) -> StatisticalMethodSummary:
    manifest, design, _ = verified_result_design(detail)
    label, function_name, statistic_type = _METHODS[design.test]
    positive, negative = _directions(design)
    correction = manifest.correction
    df = float(residual_degrees_of_freedom(design))
    values: dict[str, Any] = {
        "test": design.test.value,
        "test_label": label,
        "function_name": function_name,
        "statistic_type": statistic_type,
        "subject_count": len(design.subject_order),
        "image_count": len(design.images),
        "group_counts": dict(Counter(image.group for image in design.images if image.group)),
        "condition_order": design.condition_order,
        "covariates": [
            {"name": column.name, "centering": column.centering.value}
            for column in design.covariates
        ],
        "contrast": design.contrast,
        "tail": design.tail.value,
        "tail_label": "总体 F（无组间方向）"
        if statistic_type == "F"
        else {
            "two_sided": "双侧",
            "one_sided_positive": "正向单侧",
            "one_sided_negative": "负向单侧",
        }[design.tail.value],
        "df1": float(len(design.group_order) - 1) if statistic_type == "F" else df,
        "df2": df if statistic_type == "F" else None,
        "positive_direction": positive,
        "negative_direction": negative,
        "assumptions": (
            ("共同残差方差 GLM；独立受试者及模型残差假设需结合研究设计评估。",)
            if design.test == StatisticalTest.INDEPENDENT_TWO_SAMPLE_T
            else ("统计推断以冻结的设计、协变量编码、对比和模型假设为前提。",)
        ),
        "correction_method": correction.method if correction else None,
        "correction_evidence_status": correction_evidence_status
        if correction
        else "not_applicable",
        "software_versions": {
            name: str(software_evidence[key])[:120]
            for name, key in (
                ("MATLAB", "matlab_version"),
                ("SPM", "spm"),
                ("SPM revision", "spm_revision"),
                ("DPABI", "dpabi"),
            )
            if software_evidence is not None
            and isinstance(software_evidence.get(key), (str, int, float))
        },
        "evidence_issues": evidence_issues,
    }
    if correction is not None:
        frozen = correction.model_dump(mode="json")
        for key in ("q_threshold", "voxel_p_threshold", "cluster_p_threshold", "smoothness_mode"):
            if key in frozen:
                values[key] = frozen[key]
        if correction_evidence is not None:
            if correction_evidence.get("method") != correction.method:
                raise ValueError("correction evidence method mismatch")
            for key in ("q_threshold", "voxel_p_threshold", "cluster_p_threshold", "df1", "df2"):
                actual = correction_evidence.get(key)
                # MATLAB serializes an absent df2 as an empty array.
                actual = None if actual == [] else actual
                if key in frozen and actual != frozen[key]:
                    raise ValueError("correction evidence differs from the frozen method")
            if correction.method == "fdr":
                cutoff = correction_evidence.get("actual_p_threshold")
                values["actual_p_threshold"] = None if cutoff == [] else cutoff
            else:
                assert isinstance(correction, GrfCorrection)
                for key in ("smoothness_mode", "smoothness_dlh", "two_tailed", "tail"):
                    actual = correction_evidence.get(key)
                    actual = None if actual == [] else actual
                    expected = design.tail.value if key == "tail" else frozen[key]
                    if key in correction_evidence and actual != expected:
                        raise ValueError("GRF evidence differs from the frozen method")
                for key in (
                    "smoothness_source",
                    "effective_smoothness_dlh",
                    "effective_mask_voxel_count",
                    "minimum_cluster_size",
                    "cluster_connectivity",
                ):
                    if key in correction_evidence:
                        values[key] = correction_evidence[key]
                fwhm = correction_evidence.get("effective_fwhm")
                if fwhm is not None and fwhm != []:
                    if len(fwhm) != 3 or any(not math.isfinite(item) or item <= 0 for item in fwhm):
                        raise ValueError("GRF FWHM evidence is invalid")
                    values["effective_fwhm"] = fwhm
                source = values.get("smoothness_source")
                if source == "provided" and correction.smoothness_mode != "provided_dlh":
                    raise ValueError("GRF smoothness source mismatch")
                if (
                    source in {"header", "estimated"}
                    and correction.smoothness_mode != "dpabi_header_or_estimate"
                ):
                    raise ValueError("GRF smoothness source mismatch")
                if (
                    source == "provided"
                    and values.get("effective_smoothness_dlh") != correction.smoothness_dlh
                ):
                    raise ValueError("GRF effective DLH differs from the frozen value")
    return StatisticalMethodSummary.model_validate(values)


def _directions(design: StatisticalDesignRevision) -> tuple[str | None, str | None]:
    if design.test == StatisticalTest.ONE_WAY_ANOVA_ANCOVA:
        return None, None
    if design.test == StatisticalTest.INDEPENDENT_TWO_SAMPLE_T:
        first, second = design.group_order
        return f"{first} > {second}", f"{first} < {second}"
    if design.test == StatisticalTest.PAIRED_T:
        first, second = design.condition_order
        return f"{first} > {second}", f"{first} < {second}"
    if design.test == StatisticalTest.ONE_SAMPLE_T:
        return f"均值 > {design.one_sample_baseline:g}", f"均值 < {design.one_sample_baseline:g}"
    if design.test == StatisticalTest.CORRELATION:
        target = design.covariates[0].name
        return f"与 {target} 正相关", f"与 {target} 负相关"
    index = next(index for index, value in enumerate(design.contrast) if value)
    target = "截距" if index == 0 else design.covariates[index - 1].name
    positive, negative = f"{target} 系数 > 0", f"{target} 系数 < 0"
    return positive, negative


def result_model_facts(detail: StatisticalResultDetailView) -> dict[str, Any]:
    manifest, _, report = verified_result_design(detail)
    if detail.method_summary is None:
        return {"status": "method_summary_unavailable", "result_id": detail.result_id}
    spaces = sorted({cluster.coordinate_space for cluster in manifest.clusters})
    if manifest.non_scientific:
        localization_status = "not_assessed_synthetic"
    elif not manifest.clusters:
        localization_status = (
            "no_significant_voxels" if manifest.correction else "no_reportable_clusters"
        )
    elif any(space.lower() in {"unknown", "unreported", ""} for space in spaces):
        localization_status = "coordinate_space_unknown"
    elif spaces != ["MNI"]:
        localization_status = "coordinate_space_mismatch"
    elif not report.get("localization_plan"):
        localization_status = "atlas_not_selected"
    else:
        localization_status = "pending_atlas_verification"
    return {
        "status": "succeeded",
        "result_id": detail.result_id,
        "result_bundle_hash": detail.bundle_hash,
        "mode": detail.mode,
        "non_scientific": detail.non_scientific,
        "method": detail.method_summary.model_dump(mode="json"),
        "inference_status": manifest.inference_status.value
        if manifest.inference_status
        else "not_assessed_synthetic"
        if manifest.non_scientific
        else "uncorrected_descriptive",
        "significant_voxel_count": manifest.significant_voxel_count,
        "cluster_count": len(manifest.clusters),
        "clusters_truncated": len(manifest.clusters) > 20,
        "clusters": [
            {
                "cluster_id": item.cluster_id,
                "extent_voxels": item.extent_voxels,
                "peak_statistic": item.peak_statistic,
                "peak_coordinate_mm": item.peak_coordinate_mm,
                "coordinate_space": item.coordinate_space,
                "inference": item.inference,
                "direction": None
                if detail.method_summary.statistic_type == "F"
                else item.direction,
            }
            for item in manifest.clusters[:20]
        ],
        "localization_status": localization_status,
        "frozen_atlas": report.get("localization_plan"),
        "interpretation_limits": [
            "零显著体素表示本次检验和校正阈值下未发现显著结果，不能据此证明两组等价。",
            "总体 F 不给出哪组更高；FDR 簇是空间汇总，不是额外的簇级显著性检验。",
            "不推断疾病机制，不引用未提供的文献。",
        ],
    }


def localization_model_facts(localization: Mapping[str, Any]) -> dict[str, Any]:
    """Keep atlas facts bounded and omit any unregistered adapter fields."""
    facts = {
        key: localization.get(key)
        for key in (
            "result_id",
            "result_bundle_hash",
            "status",
            "atlas_id",
            "atlas_name",
            "atlas_content_hash",
            "coordinate_space",
            "localization_method",
            "significant_voxel_count",
        )
    }
    clusters = list(localization.get("clusters", []))
    facts.update(
        cluster_count=len(clusters),
        clusters_truncated=len(clusters) > 20,
        clusters=[
            {
                **{
                    key: item.get(key)
                    for key in (
                        "cluster_id",
                        "peak_coordinate_mm",
                        "label_value",
                        "label",
                        "distance_to_voxel_center_mm",
                    )
                },
                "coverage_count": len(item.get("coverage", [])),
                "coverage_truncated": len(item.get("coverage", [])) > 10,
                "coverage": [
                    {
                        key: region.get(key)
                        for key in ("label_value", "label", "voxel_count", "proportion")
                    }
                    for region in item.get("coverage", [])[:10]
                ],
            }
            for item in clusters[:20]
        ],
    )
    return facts


def aggregate_roi(roi: StatisticalClusterRoiView) -> dict[str, Any]:
    groups: dict[tuple[str | None, str | None], list[float]] = defaultdict(list)
    for row in roi.rows:
        if not math.isfinite(row.mean_value):
            raise ValueError("ROI mean must be finite")
        groups[(row.group, row.condition)].append(row.mean_value)
    if len(groups) > 100:
        raise ValueError("ROI groups exceed the model summary limit")
    return {
        "status": roi.status,
        "result_id": roi.result_id,
        "result_bundle_hash": roi.result_bundle_hash,
        "cluster_id": roi.cluster_id,
        "voxel_count": roi.voxel_count,
        "groups": [
            {
                "group": group,
                "condition": condition,
                "count": len(values),
                "mean": mean(values),
                "sample_standard_deviation": stdev(values) if len(values) > 1 else None,
                "minimum": min(values),
                "maximum": max(values),
            }
            for (group, condition), values in sorted(
                groups.items(), key=lambda item: (item[0][0] or "", item[0][1] or "")
            )
        ],
        "limitation": roi.limitation,
    }


def render_result_readout(facts: Mapping[str, Any]) -> str:
    if facts.get("status") != "succeeded":
        return "该结果的方法摘要暂不可核验，请查看冻结报告和运行记录。"
    if facts.get("non_scientific"):
        return "这是非科学模拟占位结果，未评估显著性或脑区定位。"
    method = facts["method"]
    lines = [
        f"{method['test_label']}，{method['tail_label']}，纳入 {method['subject_count']} 名受试者。"
    ]
    if method.get("positive_direction"):
        lines.append(
            f"正值：{method['positive_direction']}；负值：{method['negative_direction']}。"
        )
    else:
        lines.append("总体 F 检验不表示哪组更高。")
    if method.get("correction_method") == "fdr":
        cutoff = method.get("actual_p_threshold")
        cutoff_text = str(cutoff) if cutoff is not None else "未记录或未形成有效截断"
        lines.append(f"FDR q={method['q_threshold']:g}；实际 p 截断值：{cutoff_text}。")
    elif method.get("correction_method") == "grf":
        lines.append(
            f"GRF：体素 p={method['voxel_p_threshold']:g}，簇 p={method['cluster_p_threshold']:g}。"
        )
    if facts.get("inference_status") == "no_significant_voxels":
        lines.append(
            "本次检验及校正阈值下未发现显著体素（0 个），没有脑区可定位；这不能证明两组等价。"
        )
    elif facts.get("inference_status") == "significant_voxels":
        lines.append(
            f"校正后 {facts['significant_voxel_count']} 个显著体素，{facts['cluster_count']} 个簇。"
        )
    else:
        lines.append("本次未作多重比较校正，簇仅用于描述。")
    if facts.get("localization_status") == "coordinate_space_mismatch":
        lines.append("结果空间已知，但与 MNI 图谱空间不同，不能进行该图谱定位。")
    elif facts.get("localization_status") == "coordinate_space_unknown":
        lines.append("结果的坐标空间尚未确认，定位需先核实空间。")
    return "\n".join(lines)
