"""Pure draft handling; model suggestions never constitute scientific approval."""

# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Literal

from jsonschema import Draft202012Validator
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
    for name, choices in (
        ("NormalizationMode", ["disabled", "epi_template", "t1_segment", "dartel"]),
        ("HeadMotionModel", ["disabled", "rigid_6", "friston_24"]),
    ):
        original = schema["$defs"][name]
        schema["$defs"][name] = {"anyOf": [original, {"type": "string", "enum": choices}]}
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
    t1 = updated.preprocessing.get("t1")
    if isinstance(t1, dict) and "structural_artifact_id" not in t1:
        t1["structural_artifact_id"] = "workspace:anatomical-source"
    if draft_hash(updated) != draft_hash(draft):
        updated.confirmed_hash = None
    return updated


def validate_draft_patch(patch: dict[str, Any]) -> None:
    """Reject hidden fields and malformed shape, allowing incomplete scientific choices."""
    schema = draft_schema()
    schema.pop("required", None)

    def bounded(value: Any, depth: int = 0) -> None:
        if depth > 12:
            raise ValueError("draft patch nesting exceeds limit")
        if isinstance(value, dict):
            if len(value) > 100:
                raise ValueError("draft patch object exceeds limit")
            for key, child in value.items():
                if key in {"provenance", "confirmed_hash"}:
                    raise ValueError("server-owned draft field")
                bounded(child, depth + 1)
        elif isinstance(value, list):
            if len(value) > 4096:
                raise ValueError("draft patch array exceeds limit")
            for child in value:
                bounded(child, depth + 1)

    bounded(patch)

    # Partial objects are legal; semantic/numeric issues are returned with the saved draft.
    def shape_only(value: Any) -> None:
        if isinstance(value, dict):
            for name in (
                "required",
                "enum",
                "const",
                "minimum",
                "maximum",
                "exclusiveMinimum",
                "exclusiveMaximum",
                "multipleOf",
                "minItems",
                "maxItems",
                "minLength",
                "maxLength",
                "pattern",
                "minProperties",
                "maxProperties",
            ):
                value.pop(name, None)
            for child in value.values():
                shape_only(child)
        elif isinstance(value, list):
            for child in value:
                shape_only(child)

    shape_only(schema)
    if next(Draft202012Validator(schema).iter_errors(patch), None) is not None:
        raise ValueError("invalid draft patch shape")


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
            if field not in {"provenance", "t1"} and field not in values:
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


def planning_decisions(draft: WorkDraft, sources: dict[str, str] | None = None) -> dict[str, Any]:
    """Summarize draft choices without promoting suggestions to observed facts.

    The draft is a proposal.  Even a value entered by the user becomes an
    approved scientific parameter only through the existing confirmation path.
    """
    sources = sources or {}
    choices: list[dict[str, Any]] = []
    for section, values in (
        ("preprocessing", draft.preprocessing),
        ("alff_falff", draft.alff_falff),
        ("reho", draft.reho),
    ):
        if values is None:
            continue
        names = {
            "preprocessing": (
                "tr_seconds",
                "expected_time_points",
                "dummy_scans",
                "slice_timing",
                "nuisance",
                "normalization",
                "temporal_filter",
                "smoothing",
            ),
            "alff_falff": ("frequency_band", "filter_timing", "result_smoothing"),
            "reho": (
                "temporal_filter_band",
                "cluster_voxels",
                "smooth_reho",
                "global_result_smoothing",
            ),
        }[section]
        for name in names:
            path = f"{section}.{name}"
            if name not in values:
                choices.append({"path": path, "status": "missing"})
                continue
            source = sources.get(path) or next(
                (value for key, value in sources.items() if key.startswith(path + ".")),
                "unattributed",
            )
            choices.append(
                {
                    "path": path,
                    "value": values[name],
                    "source": source,
                    "status": "user_draft" if source == "user" else "proposed",
                }
            )
    issues = list(draft_issues(draft))
    for choice in choices:
        if choice["status"] == "missing":
            continue
        if any(
            issue.startswith(choice["path"] + "：") and "尚未提供" not in issue for issue in issues
        ):
            choice["status"] = "conflict"
    return {
        "version": "1",
        "provisional": True,
        "choices": choices,
        "issues": issues,
        "branches": [item["branch_id"] for item in plan_preview(draft)["branches"]],
        "note": (
            "草稿值尚不是已核实的采集事实；扫描参数须与元数据或用户明确输入核对。"
            "所有选择仍须经用户审阅和服务端科学校验。"
        ),
    }


def plan_preview(draft: WorkDraft) -> dict[str, Any]:
    """Build a deterministic, non-executable outline for Plan-and-Execute review.

    The model may suggest parameter values, but it must not be the authority for
    the workflow topology.  This preview therefore derives only from the typed
    draft and deliberately marks itself as provisional until the normal Skill
    compiler validates the complete, user-confirmed draft.
    """

    selected = {
        item.value if isinstance(item, MetricKind) else str(item)
        for item in draft.requested_metrics
    }
    preprocessing = draft.preprocessing
    smoothing = preprocessing.get("smoothing")
    smoothing_timing = smoothing.get("timing") if isinstance(smoothing, dict) else None
    common_steps: list[dict[str, Any]] = [
        {
            "step_id": "freeze_manifest",
            "label": "冻结工作区输入清单",
            "status": "planned",
            "evidence": "server_manifest_after_user_confirmation",
        },
        {
            "step_id": "common_preprocessing",
            "label": "公共 DPABI 预处理",
            "status": "planned" if preprocessing else "needs_parameters",
            "evidence": "typed_preprocessing_parameters",
        },
    ]
    t1 = preprocessing.get("t1")
    if isinstance(t1, dict):
        t1_steps = [
            {
                "step_id": "prepare_t1",
                "label": "T1 重定向与裁剪（预处理内自动执行）"
                if t1.get("preparation") == "reorient_crop"
                else "使用已准备的 T1 结构像",
                "status": "planned"
                if t1.get("preparation") == "reorient_crop"
                else "requires_verified_input",
                "evidence": "draft.preprocessing.t1",
            },
        ]
        if t1.get("coregister_to_functional"):
            t1_steps.append(
                {
                    "step_id": "coregister_t1",
                    "label": "T1 配准到功能像",
                    "status": "planned",
                    "evidence": "draft.preprocessing.t1",
                }
            )
        if t1.get("segmentation") not in {None, "disabled"}:
            t1_steps.append(
                {
                    "step_id": "segment_t1",
                    "label": "T1 " + str(t1["segmentation"]),
                    "status": "planned",
                    "evidence": "draft.preprocessing.t1",
                }
            )
        if t1.get("dartel"):
            t1_steps.append(
                {
                    "step_id": "dartel_template",
                    "label": "DARTEL 模板与逐受试者形变场",
                    "status": "planned",
                    "evidence": "frozen_subject_manifest",
                }
            )
        common_steps[1:1] = t1_steps
    if preprocessing:
        if smoothing_timing not in {None, "disabled"}:
            common_steps.append(
                {
                    "step_id": "common_smoothing",
                    "label": "公共功能像平滑",
                    "status": "review_required",
                    "evidence": "draft.preprocessing.smoothing",
                    "warning": (
                        "指标运行通常要求公共预处理保持未平滑；请改为指标结果平滑或确认适用性。"
                    ),
                }
            )
        else:
            common_steps.append(
                {
                    "step_id": "common_smoothing",
                    "label": "公共功能像平滑停用"
                    if smoothing_timing == "disabled"
                    else "公共功能像平滑待填写",
                    "status": "disabled_or_pending",
                    "evidence": "draft.preprocessing.smoothing",
                }
            )

    branches: list[dict[str, Any]] = []
    if selected & {MetricKind.ALFF.value, MetricKind.FALFF.value}:
        alff = draft.alff_falff or {}
        branches.append(
            {
                "branch_id": "alff_falff",
                "label": "ALFF/fALFF 分支",
                "consumes": "未带通滤波、未空间平滑的 verified 时序检查点",
                "steps": [
                    "calculate_alff_falff",
                    (
                        "smooth_alff_falff_result"
                        if alff.get("result_smoothing")
                        else "register_alff_falff"
                    ),
                ],
                "filter_timing": alff.get("filter_timing"),
                "status": "planned" if alff else "needs_parameters",
            }
        )
    if MetricKind.REHO.value in selected:
        reho = draft.reho or {}
        reho_steps = []
        if reho.get("temporal_filter_band") is not None:
            reho_steps.append("prepare_reho_temporal_filter_checkpoint")
        reho_steps.append("calculate_reho")
        if reho.get("smooth_reho") or reho.get("global_result_smoothing"):
            reho_steps.append("smooth_reho_result")
        else:
            reho_steps.append("register_reho")
        branches.append(
            {
                "branch_id": "reho",
                "label": "ReHo 分支",
                "consumes": "未空间平滑的 verified 时序检查点",
                "steps": reho_steps,
                "filter_timing": (
                    "metric_branch"
                    if reho.get("temporal_filter_band") is not None
                    else "disabled_or_pending"
                ),
                "status": "planned" if reho else "needs_parameters",
                "warning": (
                    "ReHo 专用平滑与全局结果平滑不能同时启用。"
                    if reho.get("smooth_reho") and reho.get("global_result_smoothing")
                    else None
                ),
            }
        )

    return {
        "kind": "dpabi_plan_preview",
        "version": "1",
        "provisional": True,
        "status": "ready_for_review" if not draft_issues(draft) else "incomplete",
        "common_steps": common_steps,
        "branches": branches,
        "shared_constraints": [
            "所有输入、参数和处理阶段必须在用户确认后由服务端重新校验。",
            "ALFF/fALFF 保留滤波前、未平滑检查点；ReHo 使用未平滑时序及其确认的分支检查点。",
            "计划预览不会生成 MATLAB 文本，也不会启动执行。",
        ],
    }


def confirmed_skill_intent(
    draft: WorkDraft,
    *,
    project_id: str,
    dataset_ref: str,
    input_manifest_hash: str,
    reviewed_hash: str,
    user_confirmation_ref: str,
    structural_artifact_id: str | None = None,
    proposal: bool = False,
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
                    "source": "proposal" if proposal else "user",
                    "evidence_ref": f"{user_confirmation_ref}#draft={reviewed_hash}",
                }
                for name in sorted(values)
            ],
        }

    preprocessing = confirmed(draft.preprocessing)
    if preprocessing is not None and structural_artifact_id is not None:
        for section in ("t1", "normalization"):
            settings = preprocessing.get(section)
            if (
                isinstance(settings, dict)
                and settings.get("structural_artifact_id") == "workspace:anatomical-source"
            ):
                settings["structural_artifact_id"] = structural_artifact_id
    return SkillPlanIntent.model_validate(
        {
            "project_id": project_id,
            "dataset_ref": dataset_ref,
            "input_manifest_hash": input_manifest_hash,
            "requested_metrics": draft.requested_metrics,
            "primary_outputs": draft.primary_outputs,
            "study_protocol_ref": draft.study_protocol_ref,
            "request_preprocessing": True,
            "preprocessing": preprocessing,
            "alff_falff": confirmed(draft.alff_falff),
            "reho": confirmed(draft.reho),
        }
    )


_LABELS = {
    "t1": "T1 结构像处理",
    "preparation": "T1 输入准备方式",
    "coregister_to_functional": "T1 配准到功能像",
    "segmentation": "组织分割方法",
    "dartel": "DARTEL 模板构建",
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


def render_draft_review(draft: WorkDraft, sources: dict[str, str] | None = None) -> str:
    """A brief human summary; complete parameters belong to the review panel."""
    from neuroagent.application.work_presentation import human_issue

    metrics = "、".join(item.value.upper() for item in draft.requested_metrics) or "预处理"
    lines = [
        f"已整理{metrics}方案草稿，可在方案区域审查和修改。",
    ]
    values = draft.preprocessing
    summary = []
    if values.get("tr_seconds") is not None:
        summary.append(f"TR {values['tr_seconds']} 秒")
    if values.get("expected_time_points") is not None:
        summary.append(f"{values['expected_time_points']} 个时间点")
    if summary:
        lines.append("、".join(summary) + "。扫描事实以读取证据为准。")
    issues = draft_issues(draft)
    if issues:
        lines.append(f"还有 {len(issues)} 项需要处理，主要问题：")
        lines.extend(f"- {human_issue(issue)}" for issue in issues[:3])
    else:
        lines.append("参数结构检查通过。完成科学校验后可确认启动计算。")
    return "\n".join(lines)


def review_issues(draft: WorkDraft, facts: dict[str, Any] | None = None) -> tuple[str, ...]:
    from neuroagent.application.work_presentation import leaf_values

    issues = list(draft_issues(draft))
    values = leaf_values(draft.model_dump(mode="json"))
    for path, fact in (facts or {}).items():
        value = values.get(path)
        if value is not None and value != fact:
            issues.append(f"{path}：当前选择 {value}，读取数据为 {fact}，请核实后修改。")
    return tuple(issues)


def draft_snapshot(
    draft: WorkDraft,
    *,
    workspace_path: str | None,
    sources: dict[str, str],
    prior: dict[str, Any] | None = None,
    facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from neuroagent.application.work_presentation import draft_delta, leaf_values

    values = draft.model_dump(mode="json")
    facts = facts or {}
    issues = review_issues(draft, facts)
    states = {
        path: "verified" if path in facts and facts[path] == value else "review_pending"
        for path, value in leaf_values(values).items()
    }
    for path in facts:
        if path in states and leaf_values(values)[path] != facts[path]:
            states[path] = "conflict"
    return {
        "draft": values,
        "hash": draft_hash(draft),
        "workspace_path": workspace_path,
        "compiled": False,
        "sources": sources,
        "facts": facts,
        "field_states": states,
        "version": int((prior or {}).get("version", 0)) + 1,
        "changes": draft_delta((prior or {}).get("draft", {}), values),
        "review_issues": list(issues),
    }
