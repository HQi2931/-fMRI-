"""Canonical path checks for read-only sources and derived workspaces."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from neuroagent.application.errors import PathPolicyError


def _resolved(path: str | Path, *, must_exist: bool) -> Path:
    candidate = Path(path).expanduser()
    try:
        return candidate.resolve(strict=must_exist)
    except (FileNotFoundError, OSError) as exc:
        raise PathPolicyError("路径不存在或无法解析。", path=str(path)) from exc


def _within(path: Path, roots: Iterable[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _overlaps(first: Path, second: Path) -> bool:
    return _within(first, (second,)) or _within(second, (first,))


class PathPolicy:
    def __init__(self, source_roots: Iterable[Path], work_root: Path) -> None:
        self.source_roots = tuple(_resolved(root, must_exist=False) for root in source_roots)
        self.work_root = _resolved(work_root, must_exist=False)
        overlaps = [root for root in self.source_roots if _overlaps(root, self.work_root)]
        if overlaps:
            raise PathPolicyError(
                "只读源根目录与派生工作根目录不能相同或互相包含。",
                path=f"{overlaps[0]} <-> {self.work_root}",
            )

    def validate_project_source_root(self, path: str | Path) -> Path:
        resolved = _resolved(path, must_exist=True)
        if not resolved.is_dir() or not _within(resolved, self.source_roots):
            raise PathPolicyError("源目录不在应用允许的只读根目录中。", path=str(path))
        return resolved

    def validate_read_path(
        self,
        path: str | Path,
        *,
        project_roots: Iterable[str | Path],
        expect_directory: bool | None = None,
    ) -> Path:
        resolved = _resolved(path, must_exist=True)
        roots = tuple(_resolved(root, must_exist=True) for root in project_roots)
        if not _within(resolved, roots) or not _within(resolved, self.source_roots):
            raise PathPolicyError("读取路径越过项目或应用允许边界。", path=str(path))
        if expect_directory is True and not resolved.is_dir():
            raise PathPolicyError("需要目录, 收到的路径不是目录。", path=str(path))
        if expect_directory is False and not resolved.is_file():
            raise PathPolicyError("需要文件, 收到的路径不是文件。", path=str(path))
        return resolved

    def validate_work_root(self, path: str | Path) -> Path:
        resolved = _resolved(path, must_exist=False)
        if not _within(resolved, (self.work_root,)):
            raise PathPolicyError("工作目录不在应用允许的派生产物根目录中。", path=str(path))
        return resolved

    def relative_source_path(self, path: Path, dataset_root: Path) -> str:
        resolved = _resolved(path, must_exist=True)
        if not _within(resolved, (dataset_root,)):
            raise PathPolicyError("扫描文件越过数据集目录。", path=str(path))
        return resolved.relative_to(dataset_root).as_posix()
