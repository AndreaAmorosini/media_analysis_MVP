"""Vietato lo shadowing del modulo ``presskg.resolution.paths``.

Un assegnamento locale ``paths = ...`` dentro una funzione che usa anche
``paths.step_dir``/``paths.root`` rende il modulo locale-ovunque:
UnboundLocalError all'uso PRIMA dell'assegnamento (accaduto davvero in
identity/profiles con la pipeline end-to-end, trovato anche in
predicates/quality_audit prima del primo run della fase predicates).
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src" / "presskg" / "resolution"

# helper storici con locale ``paths = [...]`` in funzioni che NON usano
# il modulo paths: shadowing innocuo, da risanare al prossimo giro
KNOWN_LOCAL_ONLY = {
    Path("identity/identity_gate.py"): {"load_inputs"},
    Path("identity/readiness.py"): {"load_inputs"},
}


def test_no_paths_shadowing_in_step_functions():
    offenders = []
    for phase in ("identity", "projection", "predicates", "events"):
        for py in sorted((SRC / phase).glob("*.py")):
            if py.name == "__init__.py":
                continue
            rel = py.relative_to(SRC)
            allowed = KNOWN_LOCAL_ONLY.get(rel, set())
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                shadows = any(
                    isinstance(target, ast.Name) and target.id == "paths"
                    for stmt in ast.walk(node)
                    if isinstance(stmt, ast.Assign)
                    for target in stmt.targets
                )
                uses_module = any(
                    isinstance(child, ast.Attribute)
                    and isinstance(child.value, ast.Name)
                    and child.value.id == "paths"
                    for child in ast.walk(node)
                )
                if shadows and uses_module and node.name not in allowed:
                    offenders.append(f"{rel}::{node.name}")

    assert offenders == [], (
        f"funzioni con shadowing del modulo paths (rinominare il locale): {offenders}"
    )
