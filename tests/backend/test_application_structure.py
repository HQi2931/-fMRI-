from __future__ import annotations

import ast
from pathlib import Path


def test_related_use_case_methods_stay_below_190_lines() -> None:
    root = Path(__file__).resolve().parents[2]
    files = (
        root / "neuroagent" / "application" / "service_mixins" / "statistics.py",
        root / "neuroagent" / "infrastructure" / "persistence" / "repository_mixins" / "jobs.py",
        root / "neuroagent" / "infrastructure" / "persistence" / "repository_mixins" / "qc.py",
    )
    violations: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            length = node.end_lineno - node.lineno + 1
            if length > 190:
                violations.append(f"{path.name}:{node.name}={length}")

    assert violations == []
