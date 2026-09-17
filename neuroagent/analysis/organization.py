"""Preview-only DPABI directory organization."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

DPABI_STAGE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,63}$")


class OrganizationItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_id: str = Field(min_length=1)
    source_relative_path: str = Field(min_length=1)
    target_relative_path: str = Field(min_length=1)
    role: str = Field(pattern=r"^(functional|anatomical|inventory)$")


class OrganizationPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_name: str = Field(min_length=1)
    target_stage: str = Field(
        min_length=2,
        max_length=64,
        pattern=DPABI_STAGE_PATTERN.pattern,
    )
    items: tuple[OrganizationItem, ...]
    warnings: tuple[str, ...] = ()
    blocking_issues: tuple[str, ...] = ()


def build_dpabi_preview(
    source_root: Path,
    *,
    target_stage: str,
    subjects: dict[str, dict[str, tuple[str, ...]]],
) -> OrganizationPreview:
    """Build a copy plan from manifest-relative paths without touching files."""

    if DPABI_STAGE_PATTERN.fullmatch(target_stage) is None:
        raise ValueError(
            "target_stage must be a single safe DPABI stage name (letters, digits, '_' or '-')"
        )
    items: list[OrganizationItem] = []
    blockers: list[str] = []
    seen_targets: set[str] = set()
    for subject_id in sorted(subjects):
        if subject_id in {"", ".", ".."} or "/" in subject_id or "\\" in subject_id:
            blockers.append("organization_subject_id_invalid")
            continue
        payload = subjects[subject_id]
        for role, paths in (
            ("functional", payload.get("functional", ())),
            ("anatomical", payload.get("anatomical", ())),
            ("inventory", payload.get("inventory", ())),
        ):
            for relative in sorted(paths):
                normalized = PurePosixPath(relative.replace("\\", "/"))
                if normalized.is_absolute() or ".." in normalized.parts:
                    blockers.append("organization_path_outside_manifest")
                    continue
                if not (source_root / Path(*normalized.parts)).is_file():
                    blockers.append("organization_source_missing")
                    continue
                if role == "functional":
                    target = PurePosixPath(target_stage) / subject_id / normalized.name
                elif role == "anatomical":
                    # Raw functional data conventionally pairs with T1Raw;
                    # every processed FunImg* product pairs with T1Img.
                    t1_stage = "T1Raw" if target_stage.lower().startswith("funraw") else "T1Img"
                    target = PurePosixPath(t1_stage) / subject_id / normalized.name
                else:
                    target = PurePosixPath("inventory") / subject_id / normalized
                target_text = target.as_posix()
                target_path = source_root.joinpath(*target.parts)
                source_file_path = source_root.joinpath(*normalized.parts)
                if target_path.resolve() != source_file_path.resolve() and target_path.exists():
                    blockers.append("organization_target_exists")
                    continue
                if target_text in seen_targets:
                    blockers.append("organization_target_collision")
                    continue
                seen_targets.add(target_text)
                items.append(
                    OrganizationItem(
                        subject_id=subject_id,
                        source_relative_path=normalized.as_posix(),
                        target_relative_path=target_text,
                        role=role,
                    )
                )
    if not items:
        blockers.append("organization_no_files")
    return OrganizationPreview(
        source_name=source_root.name,
        target_stage=target_stage,
        items=tuple(items),
        warnings=("preview_only_source_is_unchanged",),
        blocking_issues=tuple(dict.fromkeys(blockers)),
    )
