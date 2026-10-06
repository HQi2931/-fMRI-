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
    if re.search(
        r"(?:使用|采用|用|选择|标准化.{0,8})\s*(?:T1\s*)?Segment\b", text, re.I
    ) and not re.search(
        r"(?:不要|不|别)(?:采用|使用|用|做)?\s*(?:T1\s*)?Segment",
        text,
        re.I,
    ):
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
    if re.search(r"(?:去噪|回归).{0,40}线性趋势", text):
        set_value(patch, "preprocessing.nuisance.polynomial_trend", 1)
    for expression, tissue in (
        (r"(?:白质|WM).{0,12}均值回归", "white_matter"),
        (r"(?:脑脊液|CSF).{0,8}均值回归", "csf"),
    ):
        if re.search(expression, text, re.I):
            set_value(patch, f"preprocessing.nuisance.{tissue}.enabled", True)
            set_value(patch, f"preprocessing.nuisance.{tissue}.method", "mean")
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
        if data.get("format") == "image_header" and data.get("volume_count", 0) > 1:
            values = {
                "preprocessing.tr_seconds": data.get("tr_seconds"),
                "preprocessing.expected_time_points": data.get("volume_count"),
                "preprocessing.slice_timing.slice_count": (data.get("dimensions") or [None] * 3)[2],
            }
        elif data.get("format") == "json":
            try:
                sidecar = json.loads(data.get("content", ""))
            except (ValueError, TypeError):
                continue
            if not isinstance(sidecar, dict):
                continue
            if isinstance(sidecar.get("RepetitionTime"), (int, float)):
                values["preprocessing.tr_seconds"] = sidecar["RepetitionTime"]
            timings = sidecar.get("SliceTiming")
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
    return f"{label}：{detail}" if detail else label


def draft_delta(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    before, after = leaf_values(old), leaf_values(new)
    return [
        {"path": path, "before": before.get(path), "after": after.get(path)}
        for path in sorted(set(before) | set(after))
        if before.get(path) != after.get(path) and path != "confirmed_hash"
    ]


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
