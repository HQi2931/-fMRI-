"""Read-only structural inspection for common neuroimaging layouts."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import struct
from collections import defaultdict
from pathlib import Path
from time import perf_counter
from typing import Any

from neuroagent.application.contracts.projects import (
    DatasetProfile,
    SubjectManifestEntry,
)
from neuroagent.application.errors import InputValidationError
from neuroagent.domain.fmri.dataset import DatasetKind
from neuroagent.domain.fmri.dpabi_stages import (
    DPABI_STAGE_RULE_VERSION,
    canonical_stage_name,
    expected_anatomical_stage,
    known_stage_roots,
    stage_candidate,
    stage_rule,
)
from neuroagent.infrastructure.filesystem.path_policy import PathPolicy
from neuroagent.observability.work_timings import measure_work_stage

NIFTI_SUFFIXES = (".nii", ".nii.gz")
DICOM_SUFFIXES = (".dcm", ".ima")
DPABI_FUNCTIONAL_INPUT_STAGES = frozenset({"funraw", "funimg"})
DPABI_INVENTORY_ROOTS = frozenset({"results", "realignparameter"})
MODALITY_DIRECTORIES = frozenset({"anat", "bold", "func", "functional", "rest", "t1"})
PLAIN_FUNCTIONAL_DIRECTORIES = frozenset({"bold", "func", "functional", "rest"})
PLAIN_ANATOMICAL_DIRECTORIES = frozenset({"anat", "t1"})
PLAIN_INVENTORY_DIRECTORIES = frozenset(
    {"derivatives", "output", "outputs", "qc", "result", "results"}
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with measure_work_stage("hashing"), path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_nifti(path: Path) -> bool:
    lowered = path.name.lower()
    return lowered.endswith(NIFTI_SUFFIXES)


def _is_dicom(path: Path) -> bool:
    """Recognize conventional suffixes and extensionless Part-10 files."""

    if path.suffix.lower() in DICOM_SUFFIXES:
        return True
    try:
        if path.stat().st_size < 132:
            return False
        with path.open("rb") as stream:
            stream.seek(128)
            return stream.read(4) == b"DICM"
    except OSError:
        return False


def _nifti_header_is_readable(path: Path) -> bool:
    """Check the fixed NIfTI header marker without loading image data."""

    try:
        opener = gzip.open if path.name.lower().endswith(".nii.gz") else Path.open
        with opener(path, "rb") as stream:
            marker = stream.read(4)
        if len(marker) != 4:
            return False
        little = struct.unpack("<I", marker)[0]
        big = struct.unpack(">I", marker)[0]
        return little in {348, 540} or big in {348, 540}
    except (OSError, EOFError):
        return False


def _subject_session(path: Path, root: Path, kind: DatasetKind) -> tuple[str, str | None, str]:
    relative_parts = path.relative_to(root).parts
    if kind is DatasetKind.BIDS:
        for part in relative_parts:
            if part.startswith("sub-"):
                session = next((p for p in relative_parts if p.startswith("ses-")), None)
                return part, session, part
    # Conventional, non-BIDS trees are most often subject-first
    # (for example ``S001/func/rest.nii``).  Using the immediate parent would
    # collapse every subject into the modality name ``func``.
    if len(relative_parts) > 1 and relative_parts[0].lower() in MODALITY_DIRECTORIES:
        if len(relative_parts) < 3:
            raise InputValidationError(
                "subject_identity_ambiguous",
                "普通影像目录缺少可确定的受试者层级, 请提供显式受试者目录。",
                relative_path=Path(*relative_parts).as_posix(),
            )
        subject_part = relative_parts[1]
    else:
        subject_part = relative_parts[0] if len(relative_parts) > 1 else path.stem
    safe_subject = re.sub(r"[^A-Za-z0-9_.-]", "_", subject_part)
    return safe_subject or "unknown", None, subject_part


def _raw_bids_modality(path: Path, root: Path) -> str | None:
    relative_parts = path.relative_to(root).parts
    if len(relative_parts) == 3 and relative_parts[0].startswith("sub-"):
        return relative_parts[1].lower()
    if (
        len(relative_parts) == 4
        and relative_parts[0].startswith("sub-")
        and relative_parts[1].startswith("ses-")
    ):
        return relative_parts[2].lower()
    return None


def _bids_nifti_role(path: Path, root: Path) -> str | None:
    """Classify only raw BIDS BOLD and T1w images as scientific candidates.

    Field maps, diffusion images, masks, and derivatives remain covered by the
    immutable file inventory, but cannot silently satisfy the functional or T1
    input contract.
    """

    modality = _raw_bids_modality(path, root)
    lowered = path.name.lower()
    if modality == "func" and lowered.endswith(("_bold.nii", "_bold.nii.gz")):
        return "functional"
    if modality == "anat" and lowered.endswith(("_t1w.nii", "_t1w.nii.gz")):
        return "anatomical"
    return None


def _is_dpabi_stage_like_root(name: str) -> bool:
    lowered = name.lower()
    return bool(
        re.fullmatch(
            r"(?:s\d+_)?(?:funraw.*|funimg.*|t1raw.*|t1img.*)",
            lowered,
        )
    )


def _is_known_dpabi_inventory_stage(name: str) -> bool:
    lowered = name.lower()
    return bool(lowered in {"t1raw", "t1img"} or stage_rule(name) is not None)


def _dpabi_scientific_candidate(
    path: Path,
    root: Path,
    selected_functional_stage: str,
) -> tuple[str, str] | None:
    relative_parts = path.relative_to(root).parts
    if not relative_parts:
        return None
    stage = relative_parts[0].lower()
    anatomical_stage = expected_anatomical_stage(selected_functional_stage).casefold()
    if stage not in {selected_functional_stage, anatomical_stage}:
        return None
    if len(relative_parts) < 3:
        raise InputValidationError(
            "dpabi_subject_identity_ambiguous",
            "DPABI 输入 stage 必须使用 stage/subject/file 的显式受试者层级。",
            relative_path=Path(*relative_parts).as_posix(),
            selected_input_stage=selected_functional_stage,
        )
    subject_id = relative_parts[1]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", subject_id):
        raise InputValidationError(
            "dpabi_subject_identity_invalid",
            "DPABI 输入 stage 中的受试者目录名不是安全且唯一的 subject ID。",
            relative_path=Path(*relative_parts).as_posix(),
        )
    role = "functional" if stage == selected_functional_stage else "anatomical"
    return subject_id, role


def _plain_nifti_role(path: Path, root: Path) -> str | None:
    relative_parts = path.relative_to(root).parts
    directory_parts = tuple(part.lower() for part in relative_parts[:-1])
    if any(part in PLAIN_INVENTORY_DIRECTORIES for part in directory_parts):
        return None
    lowered_name = path.name.lower()
    if "mask" in lowered_name or any(
        token in lowered_name for token in ("alff", "falff", "reho", "vmhc")
    ):
        return None
    modality: str | None = None
    if directory_parts and directory_parts[0] in MODALITY_DIRECTORIES:
        modality = directory_parts[0]
    elif len(directory_parts) >= 2 and directory_parts[1] in MODALITY_DIRECTORIES:
        modality = directory_parts[1]
    if modality in PLAIN_FUNCTIONAL_DIRECTORIES:
        return "functional"
    if modality in PLAIN_ANATOMICAL_DIRECTORIES or lowered_name.endswith(
        ("_t1w.nii", "_t1w.nii.gz")
    ):
        return "anatomical"
    return None


class DatasetInspector:
    def __init__(self, path_policy: PathPolicy, *, max_files: int = 100_000) -> None:
        self._path_policy = path_policy
        self._max_files = max_files

    def inspect(
        self,
        source_path: Path,
        *,
        report_only: bool = False,
        selected_input_stage: str | None = None,
    ) -> dict[str, Any]:
        inspection_started = perf_counter()
        all_files = sorted((item for item in source_path.rglob("*") if item.is_file()), key=str)
        if len(all_files) > self._max_files:
            raise InputValidationError(
                "dataset_too_large_to_scan",
                "数据集文件数量超过当前安全扫描上限。",
                file_count=len(all_files),
                max_files=self._max_files,
            )
        for path in all_files:
            self._path_policy.relative_source_path(path, source_path)
        inventory_ms = (perf_counter() - inspection_started) * 1000

        nifti = [path for path in all_files if _is_nifti(path)]
        dicom = [path for path in all_files if _is_dicom(path)]
        issues: list[str] = []
        top_directories = sorted(
            (item.name for item in source_path.iterdir() if item.is_dir()), key=str.lower
        )
        top_names = {item.name.lower() for item in source_path.iterdir()}
        has_bids = "dataset_description.json" in top_names
        dpabi_stage_roots = [name for name in top_directories if _is_dpabi_stage_like_root(name)]
        has_dpabi = bool(dpabi_stage_roots or top_names.intersection(DPABI_INVENTORY_ROOTS))
        selected_dpabi_stage: str | None = None
        selected_dpabi_stage_root: str | None = None
        stage_candidates: list[dict[str, Any]] = []
        if has_bids:
            kind = DatasetKind.BIDS
        elif has_dpabi:
            kind = DatasetKind.DPABI_READY
            known_roots = known_stage_roots(dpabi_stage_roots)
            unsupported_stages = sorted(
                name
                for name in dpabi_stage_roots
                if name not in known_roots and not _is_known_dpabi_inventory_stage(name)
            )
            if unsupported_stages:
                if report_only:
                    issues.append("DPABI-ready 目录包含当前扫描契约不支持的 stage root。")
                else:
                    raise InputValidationError(
                        "dpabi_input_stage_unsupported",
                        "DPABI-ready 目录包含当前扫描契约不支持的 stage root。",
                        unsupported_stage_roots=unsupported_stages,
                        supported_input_stage_roots=[
                            supported_rule.name
                            for item in known_roots
                            if (supported_rule := stage_rule(item)) is not None
                        ],
                    )
            # Every recognized functional stage is a candidate.  Selection is
            # intentionally never inferred, even when there is only one root.
            functional_roots = [name for name in known_roots if stage_rule(name) is not None]
            selected_name = canonical_stage_name(selected_input_stage)
            if selected_input_stage is not None and selected_name is None:
                raise InputValidationError(
                    "dpabi_input_stage_unsupported",
                    "所选 DPABI 起始 stage 不在当前已核验规则中。",
                    selected_input_stage=selected_input_stage,
                    rule_version=DPABI_STAGE_RULE_VERSION,
                )
            if selected_name is not None:
                selected_dpabi_stage_root = next(
                    (
                        name
                        for name in functional_roots
                        if name.casefold() == selected_name.casefold()
                    ),
                    None,
                )
                if selected_dpabi_stage_root is None:
                    raise InputValidationError(
                        "dpabi_input_stage_not_found",
                        "所选 DPABI 起始 stage 不存在于工作区。",
                        selected_input_stage=selected_name,
                        candidate_input_stage_roots=functional_roots,
                    )
                selected_dpabi_stage = selected_dpabi_stage_root.casefold()
            elif not report_only:
                raise InputValidationError(
                    "dpabi_input_stage_selection_required",
                    "DPABI-ready 工作区必须由用户明确确认一个起始 stage。",
                    candidate_input_stage_roots=functional_roots,
                    rule_version=DPABI_STAGE_RULE_VERSION,
                )
            elif not functional_roots:
                issues.append("DPABI-ready 工作区没有可核验的功能 stage。")
            else:
                issues.append("DPABI-ready 起始 stage 尚未由用户明确确认。")
            for root_name in functional_roots:
                rule = stage_rule(root_name)
                assert rule is not None
                root_files = [
                    path
                    for path in nifti
                    if path.relative_to(source_path).parts[0].casefold() == root_name.casefold()
                ]
                stage_subjects = {
                    path.relative_to(source_path).parts[1]
                    for path in root_files
                    if len(path.relative_to(source_path).parts) >= 3
                }
                candidate_issues: list[str] = []
                if not root_files:
                    candidate_issues.append("stage 中没有可读取的 NIfTI 文件")
                if any(len(path.relative_to(source_path).parts) < 3 for path in root_files):
                    candidate_issues.append("stage 必须使用 stage/subject/file 的受试者层级")
                if any(
                    len(path.relative_to(source_path).parts) >= 3
                    and not re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9_.-]*",
                        path.relative_to(source_path).parts[1],
                    )
                    for path in root_files
                ):
                    candidate_issues.append("stage 含有不安全的受试者目录名")
                if any(not _nifti_header_is_readable(path) for path in root_files):
                    candidate_issues.append("stage 含有无法读取有效 header 的 NIfTI 文件")
                stage_candidates.append(
                    stage_candidate(
                        root_name,
                        subject_count=len(stage_subjects),
                        file_count=len(root_files),
                        issues=candidate_issues,
                    )
                )
        elif dicom and nifti:
            kind = DatasetKind.MIXED
        elif dicom:
            kind = DatasetKind.DICOM
        elif nifti:
            kind = DatasetKind.NIFTI
        else:
            kind = DatasetKind.UNKNOWN

        subject_files: dict[tuple[str, str | None], dict[str, list[str]]] = defaultdict(
            lambda: {"functional": [], "anatomical": [], "dicom": []}
        )
        normalized_subject_sources: dict[tuple[str, str | None], str] = {}
        unclassified_bids_nifti = 0
        inventory_only_dpabi_nifti = 0
        inventory_only_plain_nifti = 0
        # A frozen revision covers every regular file, including BIDS JSON
        # sidecars and participants metadata.  Otherwise a scientifically
        # meaningful source change could leave the approved manifest hash
        # unchanged.  Paths stay relative and source files are opened read-only.
        file_hashes: dict[str, str] = {}
        file_sizes: dict[str, int] = {}
        invalid_nifti_files: list[str] = []
        for path in all_files:
            relative = self._path_policy.relative_source_path(path, source_path)
            # Report-only discovery is not an executable frozen manifest.  Do
            # not reread all image bytes on every ordinary conversation turn.
            # Strict inspection and Worker preflight retain full integrity checks.
            if not report_only:
                file_hashes[relative] = sha256_file(path)
            file_sizes[relative] = path.stat().st_size
            if path in nifti and not _nifti_header_is_readable(path):
                invalid_nifti_files.append(relative)
        for path in nifti + dicom:
            relative = self._path_policy.relative_source_path(path, source_path)
            if kind is DatasetKind.DPABI_READY and selected_dpabi_stage is not None:
                try:
                    candidate = _dpabi_scientific_candidate(
                        path,
                        source_path,
                        selected_dpabi_stage,
                    )
                except InputValidationError as exc:
                    if not report_only:
                        raise
                    issues.append(exc.message)
                    if path in nifti:
                        inventory_only_dpabi_nifti += 1
                    continue
                if candidate is None:
                    if path in nifti:
                        inventory_only_dpabi_nifti += 1
                    continue
                subject_id, dpabi_bucket = candidate
                if path in dicom:
                    dpabi_bucket = "dicom"
                subject_files[(subject_id, None)][dpabi_bucket].append(relative)
                continue
            if kind is DatasetKind.DPABI_READY:
                if path in nifti:
                    inventory_only_dpabi_nifti += 1
                continue
            plain_role: str | None = None
            if kind in {DatasetKind.NIFTI, DatasetKind.MIXED} and path not in dicom:
                plain_role = _plain_nifti_role(path, source_path)
                if plain_role is None:
                    inventory_only_plain_nifti += 1
                    continue
            try:
                subject_id, session_id, source_subject_id = _subject_session(
                    path, source_path, kind
                )
            except InputValidationError as exc:
                if not report_only:
                    raise
                issues.append(exc.message)
                continue
            subject_key = (subject_id, session_id)
            if kind not in {DatasetKind.BIDS, DatasetKind.DPABI_READY}:
                previous_source_id = normalized_subject_sources.get(subject_key)
                if previous_source_id is not None and previous_source_id != source_subject_id:
                    if report_only:
                        issues.append("普通影像目录中的受试者标识清洗后发生碰撞。")
                        collision_hash = hashlib.sha256(source_subject_id.encode()).hexdigest()[:8]
                        subject_id = f"{subject_id}__collision_{collision_hash}"
                        subject_key = (subject_id, session_id)
                    else:
                        raise InputValidationError(
                            "subject_identity_collision",
                            (
                                "普通影像目录中的受试者标识清洗后发生碰撞, "
                                "请提供显式且唯一的受试者目录。"
                            ),
                            normalized_subject_id=subject_id,
                            source_subject_ids=sorted({previous_source_id, source_subject_id}),
                            candidate_relative_path=relative,
                        )
                normalized_subject_sources[subject_key] = source_subject_id
            lowered = path.name.lower()
            candidate_bucket: str | None
            if path in dicom:
                candidate_bucket = "dicom"
            elif kind is DatasetKind.BIDS:
                candidate_bucket = _bids_nifti_role(path, source_path)
                if candidate_bucket is None:
                    unclassified_bids_nifti += 1
                    if _raw_bids_modality(path, source_path) is not None:
                        subject_files.setdefault(
                            subject_key,
                            {"functional": [], "anatomical": [], "dicom": []},
                        )
                    continue
            elif plain_role is not None:
                candidate_bucket = plain_role
            elif "_t1w" in lowered or "anat" in {part.lower() for part in path.parts}:
                candidate_bucket = "anatomical"
            else:
                candidate_bucket = "functional"
            subject_files[subject_key][candidate_bucket].append(relative)

        if kind is DatasetKind.BIDS:
            # BIDS permits subject-level anatomy alongside session-level
            # functional scans.  Treat that anatomy as inherited by each
            # session and do not emit an anatomy-only pseudo-session.
            for subject_id in sorted({key[0] for key in subject_files}):
                subject_level_key = (subject_id, None)
                subject_level = subject_files.get(subject_level_key)
                if subject_level is None or not subject_level["anatomical"]:
                    continue
                session_keys = sorted(
                    (key for key in subject_files if key[0] == subject_id and key[1] is not None),
                    key=lambda key: key[1] or "",
                )
                missing_session_anatomy = [
                    key for key in session_keys if not subject_files[key]["anatomical"]
                ]
                if missing_session_anatomy and len(subject_level["anatomical"]) != 1:
                    if report_only:
                        issues.append("会话级功能像只能继承唯一的 subject-level T1。")
                    else:
                        raise InputValidationError(
                            "bids_anatomy_inheritance_ambiguous",
                            "会话级功能像只能继承唯一的 subject-level T1, 请先显式整理配对。",
                            subject_id=subject_id,
                            anatomical_files=sorted(subject_level["anatomical"]),
                        )
                for session_key in session_keys:
                    if not subject_files[session_key]["anatomical"]:
                        subject_files[session_key]["anatomical"].extend(subject_level["anatomical"])
                if session_keys and not subject_level["functional"] and not subject_level["dicom"]:
                    del subject_files[subject_level_key]

        subjects = [
            SubjectManifestEntry(
                subject_id=subject_id,
                session_id=session_id,
                functional_files=sorted(files["functional"]),
                anatomical_files=sorted(files["anatomical"]),
                dicom_files=sorted(files["dicom"]),
            )
            for (subject_id, session_id), files in sorted(
                subject_files.items(), key=lambda item: (item[0][0], item[0][1] or "")
            )
        ]
        warnings: list[str] = []
        if invalid_nifti_files:
            issues.append(f"{len(invalid_nifti_files)} 个 NIfTI 文件无法读取有效 header。")
        if any(not entry.functional_files for entry in subjects) and kind not in {
            DatasetKind.DICOM,
            DatasetKind.UNKNOWN,
        }:
            issues.append("部分受试者未识别到明确的功能 BOLD 输入。")
        if any(len(entry.functional_files) > 1 for entry in subjects):
            issues.append("部分受试者/会话存在多个功能候选。")
        if any(not entry.anatomical_files for entry in subjects) and kind not in {
            DatasetKind.DICOM,
            DatasetKind.UNKNOWN,
        }:
            issues.append("部分受试者未识别到 T1 结构像。")
        if kind is DatasetKind.UNKNOWN:
            warnings.append("未识别到 DICOM、NIfTI、BIDS 或 DPABI-ready 数据。")
        if unclassified_bids_nifti:
            warnings.append(
                f"{unclassified_bids_nifti} 个 BIDS NIfTI 未作为 BOLD/T1w 科学输入候选。"
            )
        if selected_dpabi_stage is not None:
            warnings.append(
                f"DPABI-ready 科学输入已锁定为 {selected_dpabi_stage}; "
                "其他 checkpoint 和 Results 仅保留在 inventory。"
            )
        if inventory_only_dpabi_nifti:
            warnings.append(
                f"{inventory_only_dpabi_nifti} 个其他 DPABI stage/result NIfTI 未作为科学输入候选。"
            )
        if inventory_only_plain_nifti:
            warnings.append(
                f"{inventory_only_plain_nifti} 个普通 NIfTI 缺少明确 modality 角色或属于结果/掩膜, "
                "仅保留在 inventory。"
            )
        if any(not entry.functional_files for entry in subjects):
            warnings.append("部分受试者未识别到明确的功能 BOLD 输入。")
        if kind is DatasetKind.BIDS and any(len(entry.functional_files) > 1 for entry in subjects):
            warnings.append("部分受试者/会话存在多个 BOLD run, 必须在科学计划前显式消除歧义。")
        if kind in {DatasetKind.NIFTI, DatasetKind.MIXED} and any(
            len(entry.functional_files) > 1 for entry in subjects
        ):
            warnings.append("部分受试者存在多个普通 NIfTI 功能候选, 必须在科学计划前显式消除歧义。")
        if any(entry.dicom_files for entry in subjects):
            warnings.append("DICOM 当前仅用于只读 inventory, 不能直接满足预处理或指标输入。")
        if any(not entry.anatomical_files for entry in subjects) and kind not in {
            DatasetKind.DICOM,
            DatasetKind.UNKNOWN,
        }:
            warnings.append("部分受试者未识别到 T1 结构像。")
        profile = DatasetProfile(
            kind=kind,
            file_count=len(all_files),
            nifti_count=len(nifti),
            dicom_count=len(dicom),
            subject_count=len({entry.subject_id for entry in subjects}),
            issues=issues,
            warnings=warnings,
        )
        content: dict[str, Any] = {
            "profile": profile.model_dump(mode="json"),
            "subjects": [entry.model_dump(mode="json") for entry in subjects],
            "input_stage": selected_dpabi_stage_root,
            "selected_input_stage": selected_dpabi_stage_root,
            "stage_selection_required": kind is DatasetKind.DPABI_READY
            and selected_dpabi_stage_root is None,
            "stage_candidates": stage_candidates,
            "stage_rule_version": DPABI_STAGE_RULE_VERSION,
            "issues": list(issues),
            "output_directories": sorted(
                name
                for name in top_directories
                if name.lower() in {"results", "output", "outputs", "qc", "derivatives"}
            ),
            "invalid_nifti_files": sorted(invalid_nifti_files),
            # None makes report-only content fail closed if a caller attempts
            # to use it as frozen execution evidence.  An empty dict would
            # silently pass the Worker's file-hash iteration.
            "file_hashes": None if report_only else dict(sorted(file_hashes.items())),
            "file_sizes": dict(sorted(file_sizes.items())),
        }
        if report_only:
            # A cheap change hint for planning evidence, never execution proof.
            # Full content hashes remain mandatory in frozen manifests/Worker.
            inventory = []
            for path in all_files:
                stat = path.stat()
                inventory.append(
                    (
                        path.relative_to(source_path).as_posix(),
                        stat.st_size,
                        stat.st_mtime_ns,
                        stat.st_ctime_ns,
                    )
                )
            content["metadata_revision"] = hashlib.sha256(
                json.dumps(
                    [DPABI_STAGE_RULE_VERSION, top_directories, inventory],
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            # Timings are operational telemetry, never part of the deterministic
            # frozen input content hashed by plan compilation and execution.
            content["inspection_timing_ms"] = round((perf_counter() - inspection_started) * 1000, 3)
            content["inventory_ms"] = round(inventory_ms, 3)
            content["hashing_ms"] = 0.0
        return content
