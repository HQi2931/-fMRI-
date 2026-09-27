"""Regression contract for the service/repository mixin split."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

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

# The split was meant to be behaviour-preserving, so any baseline method whose
# body changed needs an explicit, reviewed reason here.  Each entry below was
# compared against the baseline before being accepted.
INTENTIONAL_BOUNDARY_CHANGES = {
    # Inject the secret writer instead of writing the dotenv file inline.
    "create_model_profile",
    # Bind the frozen plan/manifest locks before queuing.
    "create_run",
    # Move artifact registration inside the caller's transaction.
    "_register_artifacts_in_session",
    # Fence finalization on the worker lease owner.
    "finalize_job_success",
    # Optional QC gate: adds `qc_mode`/`qc_status` to RunView.  Both are derived
    # with explicit defaults (`"require"`, and a state-based `qc_status`) so rows
    # written before the column existed still map correctly.
    "_run",
    # Optional QC gate: adds `review_scope` to QcReviewRevision, read with a
    # `"pre_statistics"` default so older QC rows stay valid.
    "_qc_review",
    # Optional QC gate: maintains the derived `qc_status` when a review is
    # approved or rejected.
    "approve_qc_review",
    # Optional QC gate: persists `review_scope`, and restricts the
    # "every included subject needs a metric artifact" check to
    # pre-statistics scope -- a head-motion-only review legitimately has no
    # metric artifacts.  The invariant that such a review can never feed
    # statistics is enforced downstream by `assert_statistics_ready`
    # (domain/fmri/qc.py), covered by
    # tests/science/test_statistics.py::test_head_motion_only_qc_cannot_enter_statistics.
    "create_qc_review",
    # Frozen manifests now expose the explicitly confirmed DPABI stage and
    # candidate rule metadata in addition to the historical subject fields.
    "_manifest",
}


def _class_methods(source: str, class_name: str) -> dict[str, str]:
    tree = ast.parse(source)
    # Transaction-aware reads intentionally replace standalone sessions. Compare
    # the remaining method bodies exactly; read-your-writes/rollback has a separate
    # behavioral regression in test_work_dialogue.py.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr == "_read_session"
        ):
            node.func = ast.Attribute(
                value=ast.Attribute(
                    value=ast.Name(id="self", ctx=ast.Load()), attr="database", ctx=ast.Load()
                ),
                attr="session_factory",
                ctx=ast.Load(),
            )
    cls = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {
        node.name: ast.dump(node, include_attributes=False)
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_repository_split_preserves_baseline_methods_exactly() -> None:
    baseline_ref = BASELINE_COMMIT
    probe = subprocess.run(
        [
            "git",
            "cat-file",
            "-e",
            f"{baseline_ref}:neuroagent/infrastructure/persistence/repository.py",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        # GitHub's PR merge checkout can omit otherwise reachable historical
        # objects; the merge base still represents the reviewed baseline.
        merge_base = subprocess.run(
            ["git", "merge-base", "HEAD", "origin/main"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if merge_base.returncode == 0:
            baseline_ref = merge_base.stdout.strip()
        else:
            # The Python quality job intentionally uses a shallow checkout;
            # its first parent may be unavailable when checkout depth is one.
            parent = subprocess.run(
                ["git", "rev-parse", "HEAD^"],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            if parent.returncode == 0:
                baseline_ref = parent.stdout.strip()
            else:
                pytest.skip("historical repository baseline is unavailable in shallow checkout")
    completed = subprocess.run(
        ["git", "show", f"{baseline_ref}:neuroagent/infrastructure/persistence/repository.py"],
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

    assert set(baseline).issubset(found)
    unchanged = set(baseline) - INTENTIONAL_BOUNDARY_CHANGES
    assert {name: found[name] for name in unchanged} == {name: baseline[name] for name in unchanged}
