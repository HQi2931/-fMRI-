"""Pure draft handling; model suggestions never constitute scientific approval."""

# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from neuroagent.application.contracts import SkillPlanIntent
from neuroagent.domain.fmri.metrics import AlffFalffParameters, MetricKind, RehoParameters
from neuroagent.domain.fmri.preprocessing import PreprocessingParameters


class WorkDraft(BaseModel):
    """Partial choices, deliberately without scientific default values."""

    model_config = ConfigDict(extra="forbid")
    preprocessing: dict[str, Any] = Field(default_factory=dict)
    alff_falff: dict[str, Any] | None = None
    reho: dict[str, Any] | None = None
    requested_metrics: list[MetricKind] = Field(default_factory=list)
    primary_outputs: list[str] = Field(default_factory=list)
    study_protocol_ref: str | None = None
    qc_mode: Literal["head_motion", "skip"] | None = None
    confirmed_hash: str | None = None

    @model_validator(mode="after")
    def reject_model_evidence(self) -> WorkDraft:
        for values in (self.preprocessing, self.alff_falff, self.reho):
            if values is not None and "provenance" in values:
                raise ValueError("草稿不能提供参数来源证明；来源由服务端在用户确认后登记")
        return self


def draft_hash(draft: WorkDraft) -> str:
    payload = draft.model_dump(mode="json", exclude={"confirmed_hash"})
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def draft_schema() -> dict[str, Any]:
    """Describe partial suggestions without exposing server-owned evidence fields."""
    schema = WorkDraft.model_json_schema()
    schema["properties"].pop("confirmed_hash", None)
    for name, model in (
        ("preprocessing", PreprocessingParameters),
        ("alff_falff", AlffFalffParameters),
        ("reho", RehoParameters),
    ):
        section = model.model_json_schema()
        section.pop("description", None)
        section["properties"].pop("provenance", None)
        for definition in section.get("$defs", {}).values():
            definition.pop("required", None)
        section.pop("required", None)
        definitions = section.pop("$defs", {})
        definitions.pop("ParameterProvenance", None)
        definitions.pop("ParameterSource", None)
        schema.setdefault("$defs", {}).update(definitions)
        schema["properties"][name] = (
            section if name == "preprocessing" else {"anyOf": [section, {"type": "null"}]}
        )

    def remove_defaults(value: Any) -> None:
        if isinstance(value, dict):
            value.pop("default", None)
            for child in value.values():
                remove_defaults(child)
        elif isinstance(value, list):
            for child in value:
                remove_defaults(child)

    remove_defaults(schema)
    return schema


def merge_work_draft(draft: WorkDraft, patch: dict[str, Any]) -> WorkDraft:
    """Recursively preserve omitted values, but respect explicit false and null."""
    if "confirmed_hash" in patch:
        raise ValueError("模型不能设置用户确认状态")

    def merge(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(old)
        for key, value in new.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = merge(result[key], value)
            else:
                result[key] = deepcopy(value)
        return result

    updated = WorkDraft.model_validate(merge(draft.model_dump(mode="json"), patch))
    if draft_hash(updated) != draft_hash(draft):
        updated.confirmed_hash = None
    return updated


def draft_issues(draft: WorkDraft) -> tuple[str, ...]:
    """Validate all choices, ignoring only the deliberately absent provenance."""
    issues: list[str] = []
    if not draft.study_protocol_ref or not draft.study_protocol_ref.strip():
        issues.append("研究方案引用（study_protocol_ref）：尚未提供")
    if draft.qc_mode is None:
        issues.append("质量控制（qc_mode）：请选择头动 QC 或明确跳过")
    if not draft.primary_outputs:
        issues.append("主要输出（primary_outputs）：尚未选择")
    selected = set(draft.requested_metrics)
    if len(selected) != len(draft.requested_metrics):
        issues.append("requested_metrics：指标不得重复")
    if bool(selected & {MetricKind.ALFF, MetricKind.FALFF}) != (draft.alff_falff is not None):
        issues.append("alff_falff：参数必须与所选 ALFF/fALFF 指标一致")
    if (MetricKind.REHO in selected) != (draft.reho is not None):
        issues.append("reho：参数必须与所选 ReHo 指标一致")
    if draft.alff_falff is not None:
        metrics = draft.alff_falff.get("requested_metrics", [])
        if (
            not isinstance(metrics, list)
            or not all(isinstance(item, str) for item in metrics)
            or set(metrics) != selected & {MetricKind.ALFF, MetricKind.FALFF}
        ):
            issues.append("alff_falff.requested_metrics：必须与方案指标选择一致")
    for name, model, values in (
        ("preprocessing", PreprocessingParameters, draft.preprocessing),
        ("alff_falff", AlffFalffParameters, draft.alff_falff),
        ("reho", RehoParameters, draft.reho),
    ):
        if values is None:
            continue
        # Even fields with domain defaults must be explicitly reviewed in a draft.
        for field in model.model_fields:
            if field != "provenance" and field not in values:
                issues.append(f"{name}.{field}：尚未提供（停用项也需明确填写 null 或 false）")
        try:
            model.model_validate({**values, "provenance": []})
        except ValidationError as exc:
            for error in exc.errors(include_input=False):
                if "scientific parameter provenance missing for:" in error["msg"]:
                    continue
                if error["type"] == "missing" and len(error["loc"]) == 1:
                    continue
                path = ".".join(str(part) for part in error["loc"])
                issues.append(f"{name}{'.' + path if path else ''}：{error['msg']}")
    return tuple(issues)


def confirmed_skill_intent(
    draft: WorkDraft,
    *,
    project_id: str,
    dataset_ref: str,
    input_manifest_hash: str,
    reviewed_hash: str,
    user_confirmation_ref: str,
) -> SkillPlanIntent:
    """Call only for an explicit user review confirmation, never model output.

    Identity, manifest and confirmation message references are trusted server
    arguments. This creates a proposal; execution still needs existing approval.
    """
    if reviewed_hash != draft_hash(draft):
        raise ValueError("草稿已变化，请重新审阅并确认")
    if not user_confirmation_ref.strip():
        raise ValueError("缺少用户明确确认的消息引用")
    issues = draft_issues(draft)
    if issues:
        raise ValueError("\n".join(issues))

    def confirmed(values: dict[str, Any] | None) -> dict[str, Any] | None:
        if values is None:
            return None
        return {
            **deepcopy(values),
            "provenance": [
                {
                    "name": name,
                    "source": "user",
                    "evidence_ref": f"{user_confirmation_ref}#draft={reviewed_hash}",
                }
                for name in sorted(values)
            ],
        }

    return SkillPlanIntent.model_validate(
        {
            "project_id": project_id,
            "dataset_ref": dataset_ref,
            "input_manifest_hash": input_manifest_hash,
            "requested_metrics": draft.requested_metrics,
            "primary_outputs": draft.primary_outputs,
            "study_protocol_ref": draft.study_protocol_ref,
            "request_preprocessing": True,
            "preprocessing": confirmed(draft.preprocessing),
            "alff_falff": confirmed(draft.alff_falff),
            "reho": confirmed(draft.reho),
        }
    )


_LABELS = {
    "options_source": "校正选项来源",
    "polynomial_trend": "多项式趋势阶数",
    "head_motion_model": "头动回归模型",
    "head_motion_scrubbing": "头动回归时间点剔除",
    "white_matter": "白质回归",
    "csf": "脑脊液回归",
    "global_signal": "全局信号回归",
    "warp_masks_to_individual_space": "掩膜变换到个体空间",
    "add_mean_back": "加回均值",
    "mask_threshold": "掩膜阈值",
    "method": "方法",
    "compcor_components": "CompCor 成分数量",
    "mode": "模式",
    "bounding_box_mm": "边界框（毫米）",
    "voxel_size_mm": "体素尺寸（毫米）",
    "structural_artifact_id": "结构像产物编号",
    "affine_regularization": "仿射正则化",
    "censoring": "时间点剔除标准",
    "fd_type": "头动 FD 类型",
    "fd_threshold_mm": "头动 FD 阈值（毫米）",
    "previous_points": "前向剔除时间点数",
    "later_points": "后向剔除时间点数",
    "preprocessing": "预处理",
    "alff_falff": "ALFF/fALFF",
    "reho": "ReHo",
    "requested_metrics": "计算指标",
    "primary_outputs": "主要输出",
    "study_protocol_ref": "研究方案引用",
    "qc_mode": "质量控制",
    "tr_seconds": "重复时间 TR（秒）",
    "expected_time_points": "预期时间点数",
    "dummy_scans": "移除初始时间点数",
    "slice_timing": "层时间校正",
    "realignment": "头动校正",
    "nuisance": "干扰回归",
    "normalization": "空间标准化",
    "detrend": "去趋势",
    "temporal_filter": "时间滤波",
    "scrubbing": "时间点剔除",
    "smoothing": "空间平滑",
    "enabled": "启用",
    "timing": "执行阶段",
    "slice_count": "层数",
    "slice_order": "采集层顺序",
    "reference_slice": "参考层",
    "frequency_band": "频段",
    "low_hz": "下限（Hz）",
    "high_hz": "上限（Hz）",
    "fwhm_mm": "半高全宽（毫米）",
    "mask_source": "掩膜来源",
    "mask_artifact_id": "掩膜产物编号",
    "requested_scalings": "输出标准化方式",
    "result_smoothing": "结果图平滑",
    "result_smoothing_fwhm_mm": "结果图平滑核（毫米）",
    "filter_timing": "滤波阶段",
    "cluster_voxels": "邻域体素数",
    "temporal_filter_band": "时间滤波频段",
    "temporal_filter_add_mean_back": "滤波后加回均值",
    "smooth_reho": "ReHo 专用平滑",
    "smooth_reho_fwhm_mm": "ReHo 平滑核（毫米）",
    "global_result_smoothing": "全局结果平滑",
    "global_result_smoothing_fwhm_mm": "全局结果平滑核（毫米）",
}


def render_draft_review(draft: WorkDraft) -> str:
    """Render every value, including nested disabled choices, for user review."""
    lines = ["方案草稿（尚未批准执行）", f"草稿校验码：{draft_hash(draft)}"]

    def render(values: dict[str, Any], depth: int = 0) -> None:
        for key, value in values.items():
            label = f"{_LABELS.get(key, key)}（{key}）" if key in _LABELS else key
            prefix = "  " * depth + "- " + label + "："
            if isinstance(value, dict):
                lines.append(prefix)
                render(value, depth + 1)
            else:
                text = (
                    "是"
                    if value is True
                    else "否"
                    if value is False
                    else (
                        "null（明确未设置/不适用；缺失项见下方）"
                        if value is None
                        else json.dumps(value, ensure_ascii=False)
                    )
                )
                lines.append(prefix + text)

    render(draft.model_dump(mode="json", exclude={"confirmed_hash"}))
    issues = draft_issues(draft)
    if issues:
        lines.append("待补充或修正：\n" + "\n".join(f"- {issue}" for issue in issues))
    else:
        lines.append("参数已完整。请审阅全部科学选择后明确确认；确认仅用于生成受控方案。")
    return "\n".join(lines)
