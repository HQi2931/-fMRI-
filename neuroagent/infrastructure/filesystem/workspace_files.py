"""Bounded, read-mostly access to user-selected workspace files.

This adapter intentionally knows nothing about the conversation or model.  It
accepts only relative paths and returns structured, locatable excerpts so the
application layer can add consent, redaction and audit policy around it.
"""

from __future__ import annotations

import csv
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any
from zipfile import BadZipFile

from docx import Document  # type: ignore[import-untyped]
from docx.opc.exceptions import PackageNotFoundError  # type: ignore[import-untyped]
from openpyxl import load_workbook  # type: ignore[import-untyped]
from openpyxl.utils.cell import get_column_letter  # type: ignore[import-untyped]
from openpyxl.utils.exceptions import InvalidFileException  # type: ignore[import-untyped]

from neuroagent.application.errors import ConflictError, InputValidationError
from neuroagent.literature.models import LiteratureParseError
from neuroagent.literature.pdf_parser import PypdfParser

SUPPORTED_TEXT_SUFFIXES = frozenset(
    {
        ".json",
        ".jsonl",
        ".ndjson",
        ".csv",
        ".tsv",
        ".txt",
        ".log",
        ".md",
        ".markdown",
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".java",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".m",
        ".sh",
        ".ps1",
        ".yaml",
        ".yml",
        ".toml",
        ".xml",
        ".ini",
        ".cfg",
        ".conf",
        ".sql",
        ".r",
        ".rs",
        ".go",
    }
)
SUPPORTED_READ_SUFFIXES = SUPPORTED_TEXT_SUFFIXES | {".pdf", ".docx", ".xlsx"}
WRITABLE_SUFFIXES = SUPPORTED_TEXT_SUFFIXES
UNSUPPORTED_BINARY_SUFFIXES = frozenset(
    {
        ".db",
        ".sqlite",
        ".sqlite3",
        ".bin",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".zip",
        ".tar",
        ".gz",
        ".7z",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".parquet",
    }
)
HARD_EXCLUDED_NAMES = frozenset(
    {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__"}
)
SOFT_EXCLUDED_NAMES = frozenset(
    {".cache", "cache", "build", "dist", "target", "coverage", "runs", "artifacts"}
)
_SECRET = re.compile(
    r"(?i)(?:bearer\s+|\b(?:api[_ -]?key|secret|token|password)\s*[:=]\s*)"
    r"[A-Za-z0-9_./+=:-]{8,}"
)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d -]{8,}\d)(?!\d)")
_ABSOLUTE = re.compile(r"(?:[A-Za-z]:[\\/][^\s\"']+|/(?:home|Users|data|mnt)/[^\s\"']+)")
_CLINICAL = re.compile(
    r"(?:患者|病人|受试者|姓名|住院号|病历号|门诊号|身份证|医保号|手机号|patient|medical\s+record)",
    re.I,
)


def sha256_file(path: Path) -> str:
    if _is_link_like(path):
        raise InputValidationError("workspace_symlink_forbidden", "不允许读取符号链接文件。")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def workspace_hash(root: Path) -> str:
    return hashlib.sha256(str(root.resolve(strict=True)).encode("utf-8")).hexdigest()


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return True
    # FILE_ATTRIBUTE_REPARSE_POINT covers Windows junctions and other
    # reparse-backed redirects; they are never traversed by this policy.
    return bool(attributes & 0x400)


@dataclass(frozen=True)
class WorkspaceRead:
    path: str
    format: str
    content: str
    locators: tuple[dict[str, Any], ...]
    sha256: str
    redactions: int = 0
    omitted_sensitive: int = 0


class WorkspaceFilePolicy:
    """Resolve, classify and read workspace files without following links."""

    def __init__(
        self,
        root: Path,
        *,
        max_text_bytes: int = 10 * 1024 * 1024,
        max_document_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise InputValidationError("workspace_not_directory", "工作区不是目录。")
        self.max_text_bytes = max_text_bytes
        self.max_document_bytes = max_document_bytes
        self.pdf_parser = PypdfParser()

    def _relative(self, value: str, *, must_exist: bool = True) -> Path:
        if (
            not value
            or "\x00" in value
            or Path(value).is_absolute()
            or "\\" in value
            or ":" in value
        ):
            # Backslashes are rejected instead of normalized to prevent Windows
            # traversal semantics from differing across API clients.
            raise InputValidationError(
                "workspace_path_invalid", "工作区文件必须是相对 POSIX 路径。"
            )
        relative = Path(value)
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise InputValidationError("workspace_path_invalid", "工作区文件路径包含非法组件。")
        target = (self.root / relative).resolve(strict=must_exist)
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise InputValidationError("workspace_path_escape", "工作区文件路径越界。") from exc
        current = self.root
        for part in relative.parts:
            current = current / part
            if _is_link_like(current):
                raise InputValidationError(
                    "workspace_symlink_forbidden", "不允许读取符号链接文件。"
                )
        if must_exist and not target.is_file():
            raise InputValidationError("workspace_file_required", "目标不是普通文件。")
        return target

    @staticmethod
    def _excluded(relative: Path, include_excluded: bool) -> bool:
        names = {part.lower() for part in relative.parts}
        if names & HARD_EXCLUDED_NAMES:
            return True
        return bool(names & SOFT_EXCLUDED_NAMES) and not include_excluded

    def list_entries(
        self,
        *,
        directory: str = "",
        include_excluded: bool = False,
        limit: int = 1000,
        offset: int = 0,
    ) -> dict[str, Any]:
        if limit < 1 or limit > 1000 or offset < 0:
            raise InputValidationError("workspace_listing_limit_invalid", "文件列表分页参数无效。")
        base = self.root if not directory else self._relative(directory, must_exist=True)
        if not base.is_dir():
            raise InputValidationError("workspace_directory_required", "目录参数不是目录。")
        entries: list[dict[str, Any]] = []
        for path in sorted(base.iterdir(), key=lambda item: item.name.casefold()):
            rel = path.relative_to(self.root)
            if self._excluded(rel, include_excluded) or _is_link_like(path):
                continue
            item = {
                "path": rel.as_posix(),
                "name": path.name,
                "kind": "directory" if path.is_dir() else "file",
            }
            if path.is_file():
                item.update(
                    {
                        "size": path.stat().st_size,
                        "format": self.classify(path),
                        "readable": self.classify(path) is not None,
                    }
                )
            entries.append(item)
        return {
            "directory": directory,
            "entries": entries[offset : offset + limit],
            "total": len(entries),
            "next_offset": offset + limit if offset + limit < len(entries) else None,
        }

    def _check_file(self, relative: str, *, include_excluded: bool = False) -> Path:
        path = self._relative(relative)
        rel = path.relative_to(self.root)
        if self._excluded(rel, include_excluded):
            raise InputValidationError("workspace_path_excluded", "该路径位于默认排除目录。")
        if not path.is_file() or _is_link_like(path):
            raise InputValidationError("workspace_file_required", "目标不是普通文件。")
        file_size = path.stat().st_size
        kind = self.classify(path)
        size_limit = self.max_text_bytes if kind in {"text", "json", "jsonl", "csv", "tsv"} else self.max_document_bytes
        if file_size > size_limit:
            raise InputValidationError("workspace_file_too_large", "文件超过读取大小限制。")
        if kind is None:
            raise InputValidationError(
                "workspace_format_unsupported", "文件格式不受支持或不是安全文本。"
            )
        return path

    @staticmethod
    def classify(path: Path) -> str | None:
        suffix = path.name.casefold().rsplit(".", 1)
        ext = f".{suffix[-1]}" if len(suffix) == 2 else ""
        if ext in UNSUPPORTED_BINARY_SUFFIXES:
            return None
        if path.name.casefold().endswith(".jsonl") or path.name.casefold().endswith(".ndjson"):
            return "jsonl"
        if ext == ".json":
            return "json"
        if ext in {".csv", ".tsv"}:
            return ext[1:]
        if ext in {".pdf", ".docx", ".xlsx"}:
            return ext[1:]
        if ext in SUPPORTED_TEXT_SUFFIXES:
            return "text"
        try:
            sample = path.read_bytes()[:4096]
            if b"\x00" not in sample:
                sample.decode("utf-8-sig")
                return "text"
        except (OSError, UnicodeDecodeError):
            pass
        return None

    @staticmethod
    def sanitize_text(text: str) -> tuple[str, int, int]:
        redactions = 0
        omitted = 0
        output: list[str] = []
        for line in text.splitlines():
            if _CLINICAL.search(line):
                output.append("[sensitive clinical line omitted]")
                omitted += 1
                continue
            original = line
            line = _SECRET.sub("[redacted-secret]", line)
            line = _EMAIL.sub("[redacted-email]", line)
            line = _PHONE.sub("[redacted-phone]", line)
            line = _ABSOLUTE.sub("[redacted-path]", line)
            redactions += original != line
            output.append(line)
        return "\n".join(output), redactions, omitted

    @staticmethod
    def _read_text(path: Path) -> str:
        if _is_link_like(path):
            raise InputValidationError("workspace_symlink_forbidden", "不允许读取符号链接文件。")
        try:
            text = path.read_text(encoding="utf-8-sig", errors="strict")
            if "\x00" in text:
                raise InputValidationError("workspace_binary_content", "文件包含 NUL 字节，不能按文本读取。")
            return text
        except UnicodeDecodeError as exc:
            raise InputValidationError(
                "workspace_encoding_invalid", "文件不是有效的 UTF-8/UTF-8-SIG 文本。"
            ) from exc

    def read(
        self,
        relative: str,
        *,
        start: int = 1,
        end: int = 400,
        sheet: str | None = None,
        include_excluded: bool = False,
    ) -> WorkspaceRead:
        if start < 1 or end < start or end - start >= 400:
            raise InputValidationError(
                "workspace_read_range_invalid", "读取范围必须是最多400行/记录。"
            )
        path = self._check_file(relative, include_excluded=include_excluded)
        kind = self.classify(path) or "text"
        digest = sha256_file(path)
        if kind == "json":
            raw = self._read_text(path)
            try:
                document = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise InputValidationError(
                    "workspace_json_parse_failed",
                    f"JSON 语法错误（第 {exc.lineno} 行，第 {exc.colno} 列）。",
                ) from exc
            lines = raw.splitlines()
            selected = lines[start - 1 : end]
            clean, redactions, omitted = self.sanitize_text("\n".join(selected))
            locators = _json_locators(raw, document, start, end)
            return WorkspaceRead(relative, kind, clean, tuple(locators), digest, redactions, omitted)
        if kind == "jsonl":
            raw = self._read_text(path)
            lines = raw.splitlines()
            selected: list[str] = []
            locators: list[dict[str, Any]] = []
            for index, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError as exc:
                    raise InputValidationError(
                        "workspace_jsonl_parse_failed",
                        f"JSONL 第 {index} 条记录语法错误（第 {exc.colno} 列）。",
                    ) from exc
                if start <= index <= end:
                    selected.append(line)
                    locators.append({"line": index, "record": index, "json_pointer": "/"})
            clean, redactions, omitted = self.sanitize_text("\n".join(selected))
            return WorkspaceRead(relative, kind, clean, tuple(locators), digest, redactions, omitted)
        if kind in {"csv", "tsv"}:
            raw = self._read_text(path)
            delimiter = "\t" if kind == "tsv" else ","
            try:
                rows = list(csv.reader(io.StringIO(raw), delimiter=delimiter, strict=True))
            except csv.Error as exc:
                raise InputValidationError("workspace_csv_parse_failed", "CSV/TSV 语法错误。") from exc
            header = rows[0] if rows else []
            selected_rows = rows[start - 1 : end]
            rendered = ["\t".join(row) for row in selected_rows]
            clean, redactions, omitted = self.sanitize_text("\n".join(rendered))
            locators = tuple(
                {
                    "line": index,
                    "row": index,
                    "columns": {
                        str(header[column]): column + 1
                        for column in range(min(len(header), len(row)))
                    },
                }
                for index, row in enumerate(selected_rows, start=start)
            )
            return WorkspaceRead(relative, kind, clean, locators, digest, redactions, omitted)
        if kind == "text":
            raw = self._read_text(path)
            lines = raw.splitlines()
            selected = lines[start - 1 : end]
            clean, redactions, omitted = self.sanitize_text("\n".join(selected))
            locators = tuple({"line": index} for index in range(start, min(end, len(lines)) + 1))
            return WorkspaceRead(relative, kind, clean, locators, digest, redactions, omitted)
        if kind == "pdf":
            try:
                parsed = self.pdf_parser.parse(path.read_bytes())
            except LiteratureParseError as exc:
                raise InputValidationError(exc.code, exc.message) from exc
            pages = [page for page in parsed.pages if start <= page.page_number <= end]
            text = "\n\n".join(f"[page {page.page_number}]\n{page.text}" for page in pages)
            clean, redactions, omitted = self.sanitize_text(text)
            return WorkspaceRead(
                relative,
                kind,
                clean,
                tuple({"page": page.page_number} for page in pages),
                digest,
                redactions,
                omitted,
            )
        if kind == "docx":
            pieces, locators = _read_docx_paragraphs(path, start, end)
            clean, redactions, omitted = self.sanitize_text("\n\n".join(pieces))
            return WorkspaceRead(
                relative, kind, clean, tuple(locators), digest, redactions, omitted
            )
        if kind == "xlsx":
            try:
                workbook = load_workbook(path, read_only=True, data_only=False)
            except (BadZipFile, InvalidFileException) as exc:
                raise InputValidationError(
                    "xlsx_parse_failed", "XLSX 文件损坏或无法解析。"
                ) from exc
            selected_sheet = sheet or workbook.sheetnames[0]
            if selected_sheet not in workbook.sheetnames:
                workbook.close()
                raise InputValidationError("xlsx_sheet_not_found", "指定工作表不存在。")
            ws = workbook[selected_sheet]
            rows: list[str] = []
            locators: list[dict[str, Any]] = []
            for row_index, row in enumerate(ws.iter_rows(values_only=True), start=1):
                if row_index < start:
                    continue
                if row_index > end:
                    break
                rows.append(
                    f"[row {row_index}]\t"
                    + "\t".join("" if value is None else str(value) for value in row)
                )
                last_column = get_column_letter(max(1, ws.max_column))
                locators.append(
                    {
                        "sheet": selected_sheet,
                        "row": row_index,
                        "range": f"A{row_index}:{last_column}{row_index}",
                    }
                )
            workbook.close()
            clean, redactions, omitted = self.sanitize_text("\n".join(rows))
            return WorkspaceRead(
                relative, kind, clean, tuple(locators), digest, redactions, omitted
            )
        raise InputValidationError("workspace_format_unsupported", "文件格式不受支持。")

    def search(
        self,
        query: str,
        *,
        glob: str = "*",
        include_excluded: bool = False,
        max_results: int = 200,
    ) -> dict[str, Any]:
        if not query or len(query) > 500 or max_results < 1 or max_results > 200:
            raise InputValidationError("workspace_search_invalid", "搜索参数无效。")
        results: list[dict[str, Any]] = []
        for path in sorted(self.root.rglob("*"), key=str):
            if len(results) >= max_results or not path.is_file() or _is_link_like(path):
                continue
            rel = path.relative_to(self.root)
            if self._excluded(rel, include_excluded) or not path.match(glob):
                continue
            if self.classify(path) not in {"text", "json", "jsonl", "csv", "tsv"}:
                continue
            try:
                content = self._read_text(path)
            except (OSError, InputValidationError):
                continue
            for index, line in enumerate(content.splitlines(), start=1):
                if query.casefold() in line.casefold():
                    if len(line) > 600:
                        position = line.casefold().find(query.casefold())
                        start = max(0, position - 280)
                        line = line[start : start + 600]
                    clean, redactions, omitted = self.sanitize_text(line)
                    results.append(
                        {
                            "path": rel.as_posix(),
                            "line": index,
                            "text": clean,
                            "redactions": redactions,
                            "omitted_sensitive": omitted,
                        }
                    )
                    if len(results) >= max_results:
                        break
        return {
            "query": query,
            "results": results,
            "total": len(results),
            "truncated": len(results) >= max_results,
            "redactions": sum(int(item["redactions"]) for item in results),
            "omitted_sensitive": sum(int(item["omitted_sensitive"]) for item in results),
        }

    def build_patch(
        self, edits: list[dict[str, Any]], *, max_files: int = 20, max_bytes: int = 1_048_576
    ) -> dict[str, Any]:
        if not edits or len(edits) > 200:
            raise InputValidationError("workspace_patch_edit_limit", "补丁编辑块数量无效。")
        normalized: list[dict[str, Any]] = []
        rendered: list[str] = []
        total = 0
        diff_total = 0
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in edits:
            relative = str(item.get("path", ""))
            grouped.setdefault(relative, []).append(item)
            replacement = str(item.get("replacement", ""))
            if "\x00" in replacement:
                raise InputValidationError("workspace_binary_content", "补丁内容不能包含 NUL 字节。")
            total += len(replacement.encode("utf-8"))
        if total > max_bytes:
            raise InputValidationError("workspace_patch_too_large", "补丁超过大小限制。")
        if len(grouped) > max_files:
            raise InputValidationError("workspace_patch_file_limit", "补丁文件数量无效。")
        for relative, file_edits in grouped.items():
            expected_values = {item.get("expected_sha256") for item in file_edits}
            if len(expected_values) != 1:
                raise InputValidationError("workspace_patch_hash_mismatch", "同一文件的原文件哈希必须一致。")
            expected = next(iter(expected_values))
            if expected is None:
                if len(file_edits) != 1:
                    raise InputValidationError("workspace_new_file_multiple_edits", "新文件只能有一个完整内容编辑块。")
                target = self._relative(relative, must_exist=False)
                if target.exists():
                    raise ConflictError("workspace_new_file_exists", "新建文件目标已经存在。")
                suffix = Path(relative).suffix.casefold()
                if suffix and suffix not in WRITABLE_SUFFIXES:
                    raise InputValidationError("workspace_format_read_only", "该文件格式只能读取，不能修改。")
                if not target.parent.is_dir() or target.parent.is_symlink():
                    raise InputValidationError("workspace_parent_invalid", "新文件父目录无效。")
                before = ""
                after = str(file_edits[0].get("replacement", ""))
            else:
                target = self._check_file(relative)
                kind = self.classify(target)
                if kind not in {"text", "json", "jsonl", "csv", "tsv"}:
                    raise InputValidationError("workspace_format_read_only", "该文件格式只能读取，不能修改。")
                if sha256_file(target) != expected:
                    raise ConflictError("workspace_file_changed", "文件已变化，补丁需要重新生成。")
                before = self._read_text(target)
                after = _apply_line_edits(before, file_edits)
                if after == before:
                    raise InputValidationError("workspace_patch_noop", "补丁不会改变文件内容。")
            diff = "".join(
                difflib.unified_diff(
                    before.splitlines(True),
                    after.splitlines(True),
                    fromfile=f"a/{relative}",
                    tofile=f"b/{relative}",
                )
            )
            diff_total += len(diff.encode("utf-8"))
            if diff_total > max_bytes:
                raise InputValidationError("workspace_patch_too_large", "补丁超过大小限制。")
            rendered.append(diff)
            after_hash = hashlib.sha256(after.encode("utf-8")).hexdigest()
            for item in file_edits:
                normalized.append(
                    {
                        "path": relative,
                        "expected_sha256": expected,
                        "start_line": item.get("start_line"),
                        "end_line": item.get("end_line"),
                        "replacement": str(item.get("replacement", "")),
                        "after_sha256": after_hash,
                    }
                )
        patch_json = json.dumps(
            normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return {
            "edits": normalized,
            "diff": "\n".join(rendered),
            "proposal_hash": hashlib.sha256(patch_json.encode("utf-8")).hexdigest(),
        }

    def apply_patch(self, edits: list[dict[str, Any]], backup_root: Path) -> dict[str, Any]:
        prepared: list[tuple[Path, str, bool]] = []
        temps: list[tuple[Path, Path]] = []
        backup_dir = backup_root / "workspace-patches" / hashlib.sha256(os.urandom(16)).hexdigest()
        backup_dir.mkdir(parents=True, exist_ok=False)
        try:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for item in edits:
                grouped.setdefault(str(item["path"]), []).append(item)
            for relative, file_edits in grouped.items():
                path = self._relative(relative, must_exist=False)
                existed = path.exists()
                expected = file_edits[0].get("expected_sha256")
                if expected is None and existed:
                    raise ConflictError("workspace_new_file_exists", "新建文件目标已经存在。")
                if expected is not None:
                    if not existed or sha256_file(path) != expected:
                        raise ConflictError("workspace_file_changed", "文件已变化，补丁未应用。")
                    if self.classify(path) not in {"text", "json", "jsonl", "csv", "tsv"}:
                        raise InputValidationError("workspace_format_read_only", "该文件格式只能读取，不能修改。")
                else:
                    suffix = Path(relative).suffix.casefold()
                    if suffix and suffix not in WRITABLE_SUFFIXES:
                        raise InputValidationError(
                            "workspace_format_read_only", "该文件格式只能读取，不能修改。"
                        )
                if existed:
                    backup = backup_dir / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, backup)
                if expected is None:
                    content = str(file_edits[0]["replacement"])
                else:
                    before = self._read_text(path)
                    content = _apply_line_edits(before, file_edits)
                prepared.append((path, content, existed))
            for path, content, _ in prepared:
                fd, name = tempfile.mkstemp(
                    prefix=f".{path.name}.", dir=str(path.parent), text=True
                )
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                    stream.write(content)
                temps.append((Path(name), path))
            for temporary, target in temps:
                os.replace(temporary, target)
            return {
                "backup_dir": str(backup_dir),
                "postimage": {
                    str(path.relative_to(self.root).as_posix()): sha256_file(path)
                    for path, _, _ in prepared
                },
            }
        except BaseException:
            for temporary, _ in temps:
                temporary.unlink(missing_ok=True)
            for path, _, existed in reversed(prepared):
                backup = backup_dir / str(path.relative_to(self.root))
                if existed and backup.exists():
                    shutil.copy2(backup, path)
                elif not existed:
                    path.unlink(missing_ok=True)
            raise


def _read_docx_paragraphs(
    path: Path, start: int, end: int
) -> tuple[list[str], list[dict[str, Any]]]:
    """Extract paragraphs and tables with python-docx without executing embeds/macros."""
    try:
        document = Document(str(path))
    except (BadZipFile, PackageNotFoundError, ValueError, OSError) as exc:
        raise InputValidationError("docx_parse_failed", "DOCX 文件损坏或无法解析。") from exc
    pieces: list[str] = []
    locators: list[dict[str, Any]] = []
    for index, paragraph in enumerate(document.paragraphs, start=1):
        text = paragraph.text.strip()
        if start <= index <= end and text:
            style_name = str(getattr(paragraph.style, "name", "") or "")
            label = style_name if style_name.casefold().startswith("heading") else "paragraph"
            pieces.append(f"[{label} {index}]\n{text}")
            locator: dict[str, Any] = {"paragraph": index}
            if label != "paragraph":
                locator["heading"] = label
            locators.append(locator)
    for table_index, table in enumerate(document.tables, start=1):
        if not start <= table_index <= end:
            continue
        for row_index, row in enumerate(table.rows, start=1):
            cells = [cell.text.strip() for cell in row.cells]
            pieces.append(f"[table {table_index}, row {row_index}]\n" + "\t".join(cells))
            locators.append(
                {"table": table_index, "row": row_index, "columns": len(cells)}
            )
    return pieces, locators


def _apply_line_edits(before: str, edits: list[dict[str, Any]]) -> str:
    lines = before.splitlines(keepends=True)
    ranges: list[tuple[int, int, str]] = []
    for item in edits:
        start = int(item.get("start_line", 0))
        end = int(item.get("end_line", 0))
        if start < 1 or end < start or end > len(lines):
            raise InputValidationError("workspace_edit_range_invalid", "补丁行范围无效。")
        ranges.append((start, end, str(item.get("replacement", ""))))
    ranges.sort(key=lambda item: item[0])
    for previous, current in pairwise(ranges):
        if current[0] <= previous[1]:
            raise InputValidationError("workspace_edit_overlap", "同一文件的补丁范围重叠。")
    result = lines[:]
    for start, end, replacement in reversed(ranges):
        result[start - 1 : end] = [replacement]
    return "".join(result)


def _json_locators(
    raw: str, document: Any, start: int, end: int
) -> list[dict[str, Any]]:
    """Return conservative source-line/JSON-Pointer evidence for JSON.

    JSON parsers do not retain source positions.  We therefore associate object
    keys and array elements with the first matching source line.  The line is
    always authoritative; the pointer is an evidence hint and never used to
    mutate a document.
    """

    lines = raw.splitlines()
    locators: list[dict[str, Any]] = []
    used: set[tuple[int, str]] = set()

    def visit(value: Any, pointer: str, cursor: int) -> int:
        if isinstance(value, dict):
            for key, child in value.items():
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                key_pattern = re.compile(rf"[\"']{re.escape(str(key))}[\"']\s*:")
                found = next(
                    (index for index in range(cursor, len(lines)) if key_pattern.search(lines[index])),
                    cursor,
                )
                child_pointer = f"{pointer}/{escaped}" if pointer else f"/{escaped}"
                if start <= found + 1 <= end and (found + 1, child_pointer) not in used:
                    locators.append({"line": found + 1, "json_pointer": child_pointer})
                    used.add((found + 1, child_pointer))
                cursor = visit(child, child_pointer, found)
            return cursor
        if isinstance(value, list):
            for index, child in enumerate(value):
                child_pointer = f"{pointer}/{index}" if pointer else f"/{index}"
                if start <= cursor + 1 <= end and (cursor + 1, child_pointer) not in used:
                    locators.append({"line": cursor + 1, "json_pointer": child_pointer})
                    used.add((cursor + 1, child_pointer))
                cursor = visit(child, child_pointer, cursor)
        return cursor

    visit(document, "", 0)
    for index in range(max(1, start), min(end, len(lines)) + 1):
        if not any(item["line"] == index for item in locators):
            locators.append({"line": index, "json_pointer": "/"})
    return sorted(locators, key=lambda item: (item["line"], item["json_pointer"]))
