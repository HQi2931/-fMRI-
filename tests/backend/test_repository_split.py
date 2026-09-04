"""Regression contract for the service/repository mixin split."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE_COMMIT = "a1ef4ae"
CONCRETE = {
    "neuroagent/infrastructure/persistence/repository.py": "SqliteRepository",
    "neuroagent/infrastructure/persistence/repository_mixins/idempotency.py": "IdempotencyMixin",
    "neuroagent/infrastructure/persistence/repository_mixins/projects.py": "ProjectDatasetMixin",
    "neuroagent/infrastructure/persistence/repository_mixins/plans.py": "PlanApprovalMixin",
    "neuroagent/infrastructure/persistence/repository_mixins/runs.py": "RunMixin",
    "neuroagent/infrastructure/persistence/repository_mixins/qc.py": "QcReviewMixin",
    "neuroagent/infrastructure/persistence/repository_mixins/jobs.py": "JobExecutionMixin",
    "neuroagent/infrastructure/persistence/repository_mixins/artifacts.py": "ArtifactEventMixin",
    "neuroagent/infrastructure/persistence/repository_mixins/models.py": "ModelAgentMixin",
}


def _class_methods(source: str, class_name: str) -> dict[str, str]:
    tree = ast.parse(source)
    cls = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {
        node.name: ast.dump(node, include_attributes=False)
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_repository_split_preserves_baseline_methods_exactly() -> None:
    completed = subprocess.run(
        ["git", "show", f"{BASELINE_COMMIT}:neuroagent/infrastructure/persistence/repository.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    baseline = _class_methods(completed.stdout, "SqliteRepository")
    found: dict[str, str] = {}
    for relative_path, class_name in CONCRETE.items():
        methods = _class_methods((ROOT / relative_path).read_text(encoding="utf-8"), class_name)
        overlap = set(found).intersection(methods)
        assert not overlap, f"duplicate repository methods: {sorted(overlap)}"
        found.update(methods)

    intentional_boundary_changes = {
        "create_run",
        "_register_artifacts_in_session",
        "finalize_job_success",
    }
    assert set(baseline).issubset(found)
    unchanged = set(baseline) - intentional_boundary_changes
    assert {name: found[name] for name in unchanged} == {name: baseline[name] for name in unchanged}
