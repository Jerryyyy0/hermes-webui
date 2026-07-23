"""Guard against runtime print() in service modules."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# Service paths scanned for bare print() calls. CLI-only modules with __main__
# blocks are allowlisted because they may still print to stdout for operators.
ALLOWLIST = {
    "api/providers.py",
    "api/session_recovery.py",
    "api/session_discoverability.py",
}


def _iter_service_py_files() -> list[Path]:
    paths: list[Path] = []
    for root_name in ("api", "integration"):
        root = REPO_ROOT / root_name
        for path in root.rglob("*.py"):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if "/tests/" in rel or rel.startswith("integration/tests/"):
                continue
            if rel.startswith("integration/scripts/"):
                continue
            paths.append(path)
    return sorted(paths)


def _prints_outside_main(tree: ast.AST) -> list[int]:
    main_nodes: set[ast.AST] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__"
        ):
            continue
        for child in ast.walk(node):
            main_nodes.add(child)

    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "print"):
            continue
        if node in main_nodes:
            continue
        hits.append(getattr(node, "lineno", 0))
    return hits


def test_service_modules_avoid_runtime_print():
    violations: list[str] = []
    for path in _iter_service_py_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        lines = _prints_outside_main(tree)
        if lines:
            violations.append(f"{rel}:{','.join(map(str, lines))}")
    assert not violations, "runtime print() found:\n" + "\n".join(violations)
