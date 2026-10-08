"""Deterministic presentation and user constraints for Work proposals."""

# ruff: noqa: RUF001
from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from typing import Any


def leaf_values(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        return {
            path: leaf
            for key, child in value.items()
            for path, leaf in leaf_values(child, f"{prefix}.{key}" if prefix else key).items()
        }
    return {prefix: value}


def set_value(target: dict[str, Any], path: str, value: Any) -> None:
    names = path.split(".")
    for name in names[:-1]:
        if not isinstance(target.get(name), dict):
            target[name] = {}
        target = target[name]
    target[names[-1]] = deepcopy(value)


def semantic_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """Map named choices centrally while retaining legacy numeric clients."""
    result = deepcopy(patch)
    maps = {
        "preprocessing.normalization.mode": {
            "disabled": 0,
            "epi_template": 1,
            "t1_segment": 2,
            "dartel": 3,
        },
        "preprocessing.nuisance.head_motion_model": {
            "disabled": 0,
            "rigid_6": 1,
            "friston_24": 4,
        },
    }
    for path, value in leaf_values(result).items():
        if path in maps and isinstance(value, str) and value in maps[path]:
            set_value(result, path, maps[path][value])
    return result


def explicit_user_patch(text: str, metrics: list[str] | None = None) -> dict[str, Any]:
    """Recognize only unambiguous current user choices, never model assertions."""
    patch: dict[str, Any] = {}
    if re.search(r"什么|为什么|区别|是否|可否", text) and not re.search(r"改为|改成|设为", text):
        return patch
    metric_section = (
        "reho" if metrics == ["reho"] and not re.search(r"\bALFF\b", text, re.I) else "alff_falff"
    )
    for expression, path, integer in (
        (
            r"\bTR\s*(?:是|为|改为|改成|设为|=|：|:)?\s*(\d+(?:\.\d+)?)",
            "preprocessing.tr_seconds",
            False,
        ),
        (
            r"(?:保留全部|全部|共|预期)\s*(\d+)\s*(?:个)?时间点",
            "preprocessing.expected_time_points",
            True,
        ),
        (
            r"时间点数(?:为|是|改为|改成)?\s*(\d+)",
            "preprocessing.expected_time_points",
            True,
        ),
        (
            r"(?:移除|删除|丢弃)(?:初始|前)?\s*(\d+)\s*(?:个)?时间点",
            "preprocessing.dummy_scans",
            True,
        ),
        (
            r"参考层(?:为|是|改为|改成|设为)?\s*(\d+)",
            "preprocessing.slice_timing.reference_slice",
            True,
        ),
    ):
        match = re.search(expression, text, re.I)
        if match:
            set_value(patch, path, int(match[1]) if integer else float(match[1]))
    if re.search(r"保留全部\s*\d+\s*(?:个)?时间点|不(?:删除|移除)初始时间点", text):
        set_value(patch, "preprocessing.dummy_scans", 0)
    voxel = re.search(
        r"MNI\s*(\d+(?:\.\d+)?)\s*mm|(?:体素(?:大小)?(?:为|是)?\s*)(\d+(?:\.\d+)?)\s*mm|(\d+(?:\.\d+)?)\s*(?:mm|毫米)体素",
        text,
        re.I,
    )
    if voxel:
        value = float(next(item for item in voxel.groups() if item is not None))
        set_value(patch, "preprocessing.normalization.voxel_size_mm", [value] * 3)
    bounds = re.search(r"范围(?:为|是)?\s*\[([^\]]+)\]\s*(?:至|到)\s*\[([^\]]+)\]", text)
    if bounds:
        try:
            box = [
                [float(value.strip()) for value in group.split(",")] for group in bounds.groups()
            ]
            if all(len(row) == 3 for row in box):
                set_value(patch, "preprocessing.normalization.bounding_box_mm", box)
        except ValueError:
            pass
    if re.search(
        r"T1.{0,30}(?:直接使用|直接用|使用已准备)|(?:直接使用|使用已准备).{0,10}T1", text, re.I
    ):
        set_value(patch, "preprocessing.t1.preparation", "prepared")
    if re.search(
        r"(?:跳过|不做|无需|不需要).{0,8}T1.{0,8}重定向|T1.{0,8}重定向.{0,8}(?:跳过|不做)",
        text,
        re.I,
    ):
        set_value(patch, "preprocessing.t1.preparation", "prepared")
    bet_choice = re.search(
        r"(不想做|不做|停用|跳过|不需要|不要|禁用|不用|启用|使用|采用|执行|做)\s*BET(?![A-Za-z0-9_])",
        text,
        re.I,
    )
    if bet_choice:
        enabled = bet_choice[1] not in {
            "不想做",
            "不做",
            "停用",
            "跳过",
            "不需要",
            "不要",
            "禁用",
            "不用",
        }
        set_value(patch, "preprocessing.t1.bet", enabled)
    if re.search(r"(?:配准到功能像|T1\s*与功能像配准|T1\s+coreg(?:ister)?\s+to\s+fun)", text, re.I):
        set_value(patch, "preprocessing.t1.coregister_to_functional", True)
    if re.search(r"标准化功能像|标准化功能时序|功能像标准化", text):
        set_value(patch, "preprocessing.normalization.timing", "on_functional_data")
    if re.search(r"(?:仿射正则化|仿射正则).{0,10}MNI", text, re.I):
        set_value(patch, "preprocessing.t1.affine_regularization", "mni")
    if re.search(r"\bNew\s*Segment\b|\bDARTEL\b", text, re.I) and not re.search(
        r"(?:不要|不|别)(?:采用|使用|用|做)?\s*(?:New\s*Segment|DARTEL)", text, re.I
    ):
        set_value(patch, "preprocessing.normalization.mode", 3)
        set_value(patch, "preprocessing.t1.segmentation", "new_segment")
        set_value(patch, "preprocessing.t1.dartel", True)
    if re.search(
        r"(?:使用|采用|用|选择|标准化.{0,8})\s*(?:T1\s*)?(?:Old\s*)?Segment\b", text, re.I
    ) and not re.search(
        r"(?:不要|不|别)(?:采用|使用|用|做)?\s*(?:T1\s*)?(?:Old\s*)?Segment",
        text,
        re.I,
    ) and not re.search(r"\bNew\s*Segment\b", text, re.I):
        set_value(patch, "preprocessing.normalization.mode", 2)
        set_value(patch, "preprocessing.t1.segmentation", "segment")
        set_value(patch, "preprocessing.t1.dartel", False)
    if re.search(r"Friston[-\s]?24", text, re.I) and not re.search(
        r"(?:不要|不|别)(?:采用|使用|用|做)?\s*Friston", text, re.I
    ):
        set_value(patch, "preprocessing.nuisance.head_motion_model", 4)
    if re.search(r"不(?:做|用|进行|采用)?\s*(?:全局信号回归|GSR\b)", text, re.I):
        set_value(
            patch,
            "preprocessing.nuisance.global_signal",
            {
                "enabled": False,
                "mask_source": None,
                "method": None,
            },
        )
    if re.search(r"(?:去噪|回归).{0,40}线性趋势|包含一次线性趋势", text):
        set_value(patch, "preprocessing.nuisance.polynomial_trend", 1)
    if re.search(r"标准化后.{0,18}(?:回归|Friston)", text, re.I):
        set_value(patch, "preprocessing.nuisance.enabled", True)
        set_value(patch, "preprocessing.nuisance.timing", "after_normalize")
    if re.search(r"不(?:另)?做线性去趋势|不(?:另外)?去趋势", text):
        set_value(patch, "preprocessing.detrend", False)
    if re.search(r"不剔除时间点|不做\s*scrubbing", text, re.I):
        set_value(patch, "preprocessing.scrubbing.enabled", False)
    if re.search(r"不把掩膜.{0,12}个体空间|不将掩膜.{0,12}个体空间", text):
        set_value(patch, "preprocessing.nuisance.warp_masks_to_individual_space", False)
    if (
        re.search(r"加回均值", text)
        and re.search(r"去噪|回归", text)
        and not re.search(r"不加回均值", text)
    ):
        set_value(patch, "preprocessing.nuisance.add_mean_back", True)
    for expression, tissue in (
        (r"(?:白质|WM).{0,12}均值回归", "white_matter"),
        (r"(?:脑脊液|CSF).{0,8}均值回归", "csf"),
    ):
        if re.search(expression, text, re.I):
            set_value(patch, f"preprocessing.nuisance.{tissue}.enabled", True)
            set_value(patch, f"preprocessing.nuisance.{tissue}.method", "mean")
    if re.search(r"组织掩膜.{0,12}(?:来自|使用).{0,8}Segment", text, re.I):
        for tissue in ("white_matter", "csf"):
            set_value(patch, f"preprocessing.nuisance.{tissue}.mask_source", "segment")
    threshold = re.search(
        r"(?:组织掩膜|白质和脑脊液|WM.{0,3}CSF).{0,30}阈值(?:均|都|为)?\s*(\d+(?:\.\d+)?)",
        text,
        re.I,
    )
    if threshold:
        for tissue in ("white_matter", "csf"):
            set_value(patch, f"preprocessing.nuisance.{tissue}.mask_threshold", float(threshold[1]))
    if re.search(r"(?:使用|用|采用)\s*DPABI\s*自动掩膜", text, re.I):
        set_value(patch, f"{metric_section}.mask_source", "dpabi_auto")
        set_value(patch, f"{metric_section}.mask_artifact_id", None)
    if re.search(r"(?:做|生成|进行)头动\s*QC", text, re.I) and not re.search(
        r"不(?:做|生成|进行)头动\s*QC", text, re.I
    ):
        set_value(patch, "qc_mode", "head_motion")
    if re.search(r"(?:明确)?跳过(?:头动)?\s*QC", text, re.I):
        set_value(patch, "qc_mode", "skip")
    if metric_section == "alff_falff":
        if re.search(r"计算前不(?:带通)?滤波|不另(?:做|行)?(?:带通)?滤波", text):
            set_value(patch, "preprocessing.temporal_filter.timing", "disabled")
            set_value(patch, "preprocessing.temporal_filter.frequency_band", None)
            set_value(patch, "preprocessing.temporal_filter.add_mean_back", None)
            set_value(patch, "alff_falff.filter_timing", "disabled")
        if re.search(r"计算前不.{0,12}空间平滑|计算前不滤波不平滑", text):
            set_value(patch, "preprocessing.smoothing.timing", "disabled")
            set_value(patch, "preprocessing.smoothing.method", None)
            set_value(patch, "preprocessing.smoothing.fwhm_mm", None)
    if (
        re.search(r"输出.*原始", text)
        and re.search(r"均值归一化", text)
        and re.search(r"z\s*标准化", text, re.I)
    ):
        set_value(patch, f"{metric_section}.requested_scalings", ["raw", "global_mean", "z_score"])
    band = re.search(
        r"(?:ALFF\s*)?频段\s*(\d+(?:\.\d+)?)\s*[-–—~至]\s*(\d+(?:\.\d+)?)\s*Hz", text, re.I
    )
    if band:
        path = "reho.temporal_filter_band" if metrics == ["reho"] else "alff_falff.frequency_band"
        set_value(
            patch,
            path,
            {"low_hz": float(band[1]), "high_hz": float(band[2])},
        )
    smooth = re.search(
        r"(?:结果(?:图)?平滑|平滑)(?:核)?(?:改为|改成|设为|设置为|采用|使用|为|\s)*"
        r"\s*(\d+(?:\.\d+)?)\s*mm\b",
        text,
        re.I,
    )
    if smooth and not re.search(r"不(?:做|用|进行)?\s*(?:结果(?:图)?)?平滑", text):
        selected = metrics or []
        if re.search(r"\bReHo\b", text, re.I) and not re.search(r"\b(?:ALFF|fALFF)\b", text, re.I):
            selected = ["reho"]
        elif re.search(r"\b(?:ALFF|fALFF)\b", text, re.I):
            selected = ["alff"]
        for section in ["reho"] if selected == ["reho"] else ["alff_falff"]:
            name = "smooth_reho" if section == "reho" else "result_smoothing"
            set_value(patch, f"{section}.{name}", True)
            set_value(patch, f"{section}.{name}_fwhm_mm", [float(smooth[1])] * 3)
    # A plain request for ALFF does not authorize adding fALFF/ReHo. Explanatory
    # questions and negated requests are deliberately not parsed as choices.
    if re.search(
        r"(?:计算|完成|做|运行|只要|仅做|只做).{0,6}\bALFF\b", text, re.I
    ) and not re.search(
        r"fALFF|ReHo|(?:不要|不计算|不做)\s*ALFF|什么|为何|为什么",
        text,
        re.I,
    ):
        set_value(patch, "requested_metrics", ["alff"])
        set_value(patch, "alff_falff.requested_metrics", ["alff"])
        set_value(patch, "reho", None)
    return patch


def constrained_patch(
    old: dict[str, Any],
    sources: dict[str, str],
    proposed: dict[str, Any],
    text: str,
) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    patch = semantic_patch(proposed)
    user = explicit_user_patch(
        text, old.get("requested_metrics") or proposed.get("requested_metrics")
    )
    if (user.get("reho") or {}).get("smooth_reho") and (old.get("reho") or {}).get(
        "global_result_smoothing"
    ):
        change = user["reho"]
        change["global_result_smoothing"] = change.pop("smooth_reho")
        change["global_result_smoothing_fwhm_mm"] = change.pop("smooth_reho_fwhm_mm")
    old_values = leaf_values(old)
    user_values = leaf_values(user)
    proposed_values = leaf_values(patch)
    blocked: list[str] = []
    # Restore the entire protected leaf even if the model replaces its parent
    # with null. Only a direct user choice or the parameter editor can unlock it.
    merged = deepcopy(old)

    def merge(target: dict[str, Any], changes: dict[str, Any]) -> None:
        for key, value in changes.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                merge(target[key], value)
            else:
                target[key] = deepcopy(value)

    merge(merged, patch)
    for path, origin in sources.items():
        if origin != "user" or path not in old_values or path in user_values:
            continue
        if leaf_values(merged).get(path) != old_values[path]:
            set_value(patch, path, old_values[path])
            blocked.append(path)
    for path, value in user_values.items():
        if path in proposed_values and proposed_values[path] != value and path not in blocked:
            blocked.append(path)
    merge(patch, user)
    updated_sources = dict(sources)
    for path, value in leaf_values(patch).items():
        if path in user_values:
            updated_sources[path] = "user"
        elif value != old_values.get(path) or path not in updated_sources:
            updated_sources[path] = "suggested"
    return patch, updated_sources, blocked


def observed_facts(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Only accept successful numeric headers and parsed sidecars in this turn."""
    observations: dict[str, list[Any]] = {}
    for call in (metadata or {}).get("calls", []):
        if call.get("tool_name") != "read_workspace_content" or call.get("status") != "succeeded":
            continue
        data = call.get("output") or {}
        values: dict[str, Any] = {}
        count = data.get("volume_count")
        if (
            data.get("format") == "image_header"
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count > 1
        ):
            values["preprocessing.expected_time_points"] = count
            tr = data.get("tr_seconds")
            if (
                isinstance(tr, (int, float))
                and not isinstance(tr, bool)
                and math.isfinite(tr)
                and tr > 0
            ):
                values["preprocessing.tr_seconds"] = tr
            dimensions = data.get("dimensions")
            if isinstance(dimensions, list) and len(dimensions) >= 3:
                slices = dimensions[2]
                if isinstance(slices, int) and not isinstance(slices, bool) and slices > 0:
                    values["preprocessing.slice_timing.slice_count"] = slices
        elif data.get("format") == "json":
            sidecar = data.get("numeric_metadata")
            if not isinstance(sidecar, dict):
                try:
                    sidecar = json.loads(data.get("content", ""))
                except (ValueError, TypeError):
                    continue
            if not isinstance(sidecar, dict):
                continue
            timings = sidecar.get("SliceTiming")
            # Anatomical sidecars also contain RepetitionTime. Bind functional
            # facts to slice metadata or the trusted functional-stage path.
            functional = isinstance(timings, list) and bool(timings)
            functional = functional or bool(
                re.search(r"(?:^|/)(?:FunRaw|FunImg[A-Z]*|func)(?:/|$)", str(data.get("path", "")))
            )
            tr = sidecar.get("RepetitionTime")
            if (
                functional
                and isinstance(tr, (int, float))
                and not isinstance(tr, bool)
                and math.isfinite(tr)
                and tr > 0
            ):
                values["preprocessing.tr_seconds"] = tr
            if (
                isinstance(timings, list)
                and timings
                and all(
                    isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                    for v in timings
                )
            ):
                values["preprocessing.slice_timing.slice_count"] = len(timings)
                values["preprocessing.slice_timing.slice_order"] = [
                    index + 1
                    for index in sorted(range(len(timings)), key=lambda i: (timings[i], i))
                ]
        for path, value in values.items():
            if value is not None:
                observations.setdefault(path, []).append(value)
    # Multiple inconsistent acquisitions require per-scan handling; do not
    # silently choose the last file for a shared draft.
    return {
        path: values[0]
        if all(v == values[0] for v in values)
        else {"conflicting_values": list(dict.fromkeys(json.dumps(v) for v in values))}
        for path, values in observations.items()
    }


def merge_observed_facts(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Accumulate acquisition evidence; one reread cannot clear known conflicts."""
    merged = deepcopy(previous)
    for path, value in current.items():
        if path not in merged:
            merged[path] = deepcopy(value)
            continue
        candidates: list[Any] = []
        for observation in (merged[path], value):
            if isinstance(observation, dict) and "conflicting_values" in observation:
                readings = [json.loads(item) for item in observation["conflicting_values"]]
            else:
                readings = [observation]
            for reading in readings:
                if reading not in candidates:
                    candidates.append(reading)
        merged[path] = (
            candidates[0]
            if len(candidates) == 1
            else {"conflicting_values": [json.dumps(item) for item in candidates]}
        )
    return merged


def human_issue(issue: str) -> str:
    from neuroagent.application.work_draft import _LABELS

    path, _, detail = issue.partition("：")
    path = re.sub(r"（[^）]*）", "", path)
    label = " / ".join(_LABELS.get(part, part) for part in path.split("."))
    if "尚未提供" in detail:
        detail = "尚未填写，请在方案中补充。"
    if "请选择" in detail:
        detail = "请选择头动质量控制或明确跳过。"
    if "greater than 0" in detail:
        detail = "必须大于 0。"
    if "scientific parameter provenance" in detail:
        detail = "缺少可靠的参数来源。"
    if "Field required" in detail:
        detail = "尚未填写，请在方案中补充。"
    if "Input should be None" in detail:
        detail = "此参数所属步骤已停用，请清除不适用的值。"
    if "Input should be a valid" in detail:
        detail = "输入格式不符合要求，请检查参数类型与范围。"
    return f"{label}：{detail}" if detail else label


def draft_delta(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    before, after = leaf_values(old), leaf_values(new)
    return [
        {"path": path, "before": before.get(path), "after": after.get(path)}
        for path in sorted(set(before) | set(after))
        if before.get(path) != after.get(path) and path != "confirmed_hash"
    ]


def pending_review_questions(
    questions: list[dict[str, Any] | str],
    values: dict[str, Any],
    sources: dict[str, str],
    facts: dict[str, Any],
) -> list[str]:
    """Hide questions already settled by user choices or matching read evidence."""
    leaves = leaf_values(values)
    pending: list[str] = []
    aliases = (
        (r"\bTR\b|重复时间", "preprocessing.tr_seconds"),
        (r"参考层", "preprocessing.slice_timing.reference_slice"),
        (r"初始|删除|丢弃", "preprocessing.dummy_scans"),
        (r"结果.*平滑|平滑.*结果", "alff_falff.result_smoothing_fwhm_mm"),
        (r"全局信号|GSR", "preprocessing.nuisance.global_signal.enabled"),
    )
    for question in questions:
        text = question if isinstance(question, str) else str(question.get("text", ""))
        if re.search(r"研究方案引用|study_protocol_ref|protocol reference|协议编号", text, re.I):
            continue
        paths = [] if isinstance(question, str) else question.get("parameter_paths", [])
        if not paths:
            paths = [path for pattern, path in aliases if re.search(pattern, text, re.I)]
        settled = bool(paths) and all(
            path in leaves
            and leaves[path] is not None
            and (path not in facts or leaves[path] == facts[path])
            and (
                sources.get(path) == "user"
                or (path in facts and leaves[path] == facts[path])
                or (sources.get(path) == "system" and path.endswith(".structural_artifact_id"))
            )
            for path in paths
        )
        text = text.strip()
        if not settled and text and text not in pending:
            pending.append(text[:400])
    return pending


def complete_input_bindings(values: dict[str, Any]) -> dict[str, Any]:
    """Reuse the chosen T1 input for Segment normalization when no override exists."""
    result = deepcopy(values)
    preprocessing = result.get("preprocessing") or {}
    normalization = preprocessing.get("normalization")
    t1 = preprocessing.get("t1")
    if isinstance(t1, dict):
        t1.setdefault("structural_artifact_id", "workspace:anatomical-source")
    if (
        isinstance(normalization, dict)
        and normalization.get("mode") == 2
        and "structural_artifact_id" not in normalization
        and isinstance(t1, dict)
        and isinstance(t1.get("structural_artifact_id"), str)
        and t1["structural_artifact_id"]
    ):
        normalization["structural_artifact_id"] = t1["structural_artifact_id"]
    return result


def complete_inactive_parameters(values: dict[str, Any]) -> dict[str, Any]:
    """Fill absent inactive slots only; retain conflicting explicit settings."""
    result = deepcopy(values)
    preprocessing = result.get("preprocessing") or {}
    slots: tuple[str, ...]

    def inactive(config: Any, names: tuple[str, ...]) -> None:
        if isinstance(config, dict):
            for name in names:
                config.setdefault(name, None)

    for name, slots in (
        ("slice_timing", ("slice_count", "slice_order", "reference_slice")),
        ("realignment", ("options_source",)),
        ("scrubbing", ("timing", "censoring", "method")),
        (
            "nuisance",
            (
                "timing",
                "polynomial_trend",
                "head_motion_model",
                "head_motion_scrubbing",
                "white_matter",
                "csf",
                "global_signal",
                "warp_masks_to_individual_space",
                "add_mean_back",
            ),
        ),
    ):
        config = preprocessing.get(name)
        if isinstance(config, dict) and config.get("enabled") is False:
            inactive(config, slots)
    for name, slots in (
        ("temporal_filter", ("frequency_band", "add_mean_back")),
        ("smoothing", ("method", "fwhm_mm")),
    ):
        config = preprocessing.get(name)
        if isinstance(config, dict) and config.get("timing") == "disabled":
            inactive(config, slots)
    nuisance = preprocessing.get("nuisance") or {}
    for name in ("white_matter", "csf"):
        tissue = nuisance.get(name)
        if isinstance(tissue, dict):
            if tissue.get("enabled") is False:
                inactive(tissue, ("mask_source", "mask_threshold", "method", "compcor_components"))
            elif tissue.get("method") == "mean":
                inactive(tissue, ("compcor_components",))
    signal = nuisance.get("global_signal")
    if isinstance(signal, dict) and signal.get("enabled") is False:
        inactive(signal, ("mask_source", "method"))
    for name in ("alff_falff", "reho"):
        config = result.get(name)
        if isinstance(config, dict):
            if config.get("mask_source") == "dpabi_auto":
                inactive(config, ("mask_artifact_id",))
            for flag in ("result_smoothing", "smooth_reho", "global_result_smoothing"):
                if config.get(flag) is False:
                    inactive(config, (f"{flag}_fwhm_mm",))
    return result
