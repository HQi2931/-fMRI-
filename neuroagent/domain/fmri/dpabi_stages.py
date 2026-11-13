"""Versioned, conservative DPABI functional-stage rules.

The directory suffixes are produced by ``DPARSFA_run.m`` as processing steps
complete.  This module deliberately contains only the small set of stage
names verified against the configured DPABI source; a ``FunImg`` prefix alone
is not sufficient evidence that a directory is a resumable checkpoint.
"""

from __future__ import annotations

from dataclasses import dataclass

DPABI_STAGE_RULE_VERSION = "dpabi-v9.0-stage-r1"
DPABI_VERIFIED_SOURCE_VERSION = "V9.0_250415"


@dataclass(frozen=True, slots=True)
class DpabiStageRule:
    name: str
    label: str
    completed_steps: tuple[str, ...]
    requires_realign_parameters: bool = False


_RULES = (
    DpabiStageRule("FunRaw", "原始功能输入", ()),
    DpabiStageRule("FunImg", "NIfTI 功能输入", ()),
    DpabiStageRule("FunImgA", "已完成时间层校正", ("slice_timing",)),
    DpabiStageRule("FunImgAR", "已完成时间层校正和头动校正", ("slice_timing", "realignment"), True),
    DpabiStageRule(
        "FunImgARW",
        "已完成时间层校正、头动校正和标准化",
        ("slice_timing", "realignment", "normalization"),
        True,
    ),
    DpabiStageRule(
        "FunImgARWS",
        "已完成时间层校正、头动校正、标准化和平滑",
        ("slice_timing", "realignment", "normalization", "smoothing"),
        True,
    ),
    DpabiStageRule(
        "FunImgARC",
        "已完成时间层校正、头动校正和协变量回归",
        ("slice_timing", "realignment", "nuisance"),
        True,
    ),
    DpabiStageRule(
        "FunImgARCF",
        "已完成时间层校正、头动校正、协变量回归和滤波",
        ("slice_timing", "realignment", "nuisance", "filter"),
        True,
    ),
)

RULES_BY_KEY = {rule.name.casefold(): rule for rule in _RULES}


def canonical_stage_name(value: str | None) -> str | None:
    if value is None:
        return None
    rule = RULES_BY_KEY.get(value.strip().casefold())
    return rule.name if rule is not None else None


def stage_rule(value: str | None) -> DpabiStageRule | None:
    canonical = canonical_stage_name(value)
    return RULES_BY_KEY.get(canonical.casefold()) if canonical else None


def is_known_stage(value: str) -> bool:
    return stage_rule(value) is not None


def verified_source_version(value: str | None) -> bool:
    """Return whether the configured DPABI source matches the locked rule table."""

    if not value:
        return False
    normalized = value.casefold().replace(" ", "")
    return normalized in {
        DPABI_VERIFIED_SOURCE_VERSION.casefold(),
        f"dpabi{DPABI_VERIFIED_SOURCE_VERSION}".casefold(),
        f"dpabi-{DPABI_VERIFIED_SOURCE_VERSION}".casefold(),
        f"dpabi_{DPABI_VERIFIED_SOURCE_VERSION}".casefold(),
    }


def known_stage_roots(names: list[str] | tuple[str, ...]) -> list[str]:
    """Return actual directory names whose case-insensitive name is verified."""

    return sorted((name for name in names if is_known_stage(name)), key=str.casefold)


def expected_anatomical_stage(value: str) -> str:
    rule = stage_rule(value)
    if rule is None:
        raise ValueError(f"unknown DPABI stage: {value}")
    return "T1Raw" if rule.name.casefold() == "funraw" else "T1Img"


def stage_candidate(
    rule_name: str,
    *,
    subject_count: int,
    file_count: int,
    issues: list[str] | None = None,
) -> dict[str, object]:
    rule = stage_rule(rule_name)
    if rule is None:
        raise ValueError(f"unknown DPABI stage: {rule_name}")
    return {
        "stage": rule.name,
        "label": rule.label,
        "completed_steps": list(rule.completed_steps),
        "requires_realign_parameters": rule.requires_realign_parameters,
        "subject_count": subject_count,
        "file_count": file_count,
        "selectable": not issues,
        "issues": list(issues or []),
        "rule_version": DPABI_STAGE_RULE_VERSION,
    }
