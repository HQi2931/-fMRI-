from __future__ import annotations

import ast
from pathlib import Path

APPLICATION_ROOT = Path(__file__).resolve().parents[2] / "neuroagent" / "application"
FORBIDDEN_MODULE_PREFIXES = (
    "sqlalchemy",
    "subprocess",
    "neuroagent.infrastructure",
    "neuroagent.workflow.worker",
)
FORBIDDEN_SYMBOLS = {"SQLiteWorker", "MockJobExecutor"}


def test_application_package_depends_only_on_ports_not_infrastructure() -> None:
    violations: list[str] = []
    for path in sorted(APPLICATION_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: tuple[str, ...] = ()
            imported_symbols: set[str] = set()
            if isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules = (node.module or "",)
                imported_symbols = {alias.name for alias in node.names}
            else:
                continue
            for module in modules:
                if any(
                    module == prefix or module.startswith(f"{prefix}.")
                    for prefix in FORBIDDEN_MODULE_PREFIXES
                ):
                    violations.append(f"{path.relative_to(APPLICATION_ROOT)} imports {module}")
            forbidden_symbols = imported_symbols.intersection(FORBIDDEN_SYMBOLS)
            if forbidden_symbols:
                violations.append(
                    f"{path.relative_to(APPLICATION_ROOT)} imports "
                    f"{', '.join(sorted(forbidden_symbols))}"
                )

    assert violations == []
