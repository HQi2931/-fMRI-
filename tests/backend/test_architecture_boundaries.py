"""Guard the capability boundaries used by the application and web layers."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _python_files(*roots: Path) -> list[Path]:
    return [path for root in roots for path in root.rglob("*.py")]


def test_application_contracts_are_imported_from_their_capability_modules() -> None:
    offenders: list[str] = []
    for path in _python_files(ROOT / "neuroagent", ROOT / "tests", ROOT / "scripts"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            aggregate_import = isinstance(node, ast.ImportFrom) and (
                node.module == "neuroagent.application.contracts"
            )
            aggregate_import |= isinstance(node, ast.Import) and any(
                alias.name == "neuroagent.application.contracts" for alias in node.names
            )
            if aggregate_import:
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_capability_repository_ports_do_not_own_shared_stores() -> None:
    source = (ROOT / "neuroagent/application/repository_ports.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    violations: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or not node.name.endswith("RepositoryPort"):
            continue
        if node.name == "RepositoryPort":
            continue
        bases = {
            base.id if isinstance(base, ast.Name) else base.attr
            for base in node.bases
            if isinstance(base, (ast.Name, ast.Attribute))
        }
        if bases.intersection({"IdempotencyStorePort", "EventRepositoryPort"}):
            violations.append(node.name)
    assert violations == []


def test_application_use_cases_do_not_import_aggregate_ports() -> None:
    modules = (
        ROOT / "neuroagent/application/conversation_context.py",
        ROOT / "neuroagent/application/conversation_work.py",
        ROOT / "neuroagent/application/service_mixins/skills.py",
        ROOT / "neuroagent/application/service_mixins/runs.py",
        ROOT / "neuroagent/application/service_mixins/statistics.py",
    )
    forbidden = {
        "RepositoryPort",
        "ConversationContextRepositoryPort",
        "ConversationWorkRepositoryPort",
        "SkillRepositoryPort",
    }
    violations: list[str] = []
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "neuroagent.application.repository_ports"
            for alias in node.names
        }
        violations.extend(
            f"{path.relative_to(ROOT)}:{name}" for name in imported.intersection(forbidden)
        )
    assert violations == []


def test_run_data_port_does_not_include_other_capability_operations() -> None:
    source = (ROOT / "neuroagent/application/repository_ports.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    classes = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name in {"RunRepositoryPort", "ArtifactCatalogRepositoryPort", "QcRepositoryPort"}
    }

    def method_names(node: ast.ClassDef) -> set[str]:
        return {
            member.name
            for member in node.body
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    artifact_methods = method_names(classes["ArtifactCatalogRepositoryPort"])
    run_methods = method_names(classes["RunRepositoryPort"])
    qc_methods = method_names(classes["QcRepositoryPort"])
    assert artifact_methods.isdisjoint(run_methods)
    assert qc_methods.isdisjoint(run_methods)
    assert {"create_qc_review", "approve_qc_review", "get_approved_qc_review"}.issubset(qc_methods)
    assert {
        "register_artifacts",
        "list_artifacts",
        "get_artifact",
        "create_statistical_result",
        "get_statistical_result",
    }.issubset(artifact_methods)


def test_production_web_features_do_not_import_the_aggregate_api_client() -> None:
    offenders: list[str] = []
    paths = [*(ROOT / "web/src").rglob("*.ts"), *(ROOT / "web/src").rglob("*.tsx")]
    for path in paths:
        if path.name.endswith(".test.ts") or path.name.endswith(".test.tsx"):
            continue
        if path == ROOT / "web/src/api/client.ts":
            continue
        source = path.read_text(encoding="utf-8")
        if re.search(r'from ["\'][^"\']*(?:/api/client|/client)["\']', source):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
