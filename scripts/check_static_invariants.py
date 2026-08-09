"""Fail-closed static smoke gates that are too structural for unit tests."""

from __future__ import annotations

import ast
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _python_files() -> list[Path]:
    return sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "apps").rglob("*.py"))


def _numeric_expression(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, float)
    if isinstance(node, ast.Call):
        name = node.func.id if isinstance(node.func, ast.Name) else ""
        return name in {"float", "sum", "min", "max", "abs", "round"}
    return False


def _source_violations() -> list[str]:
    violations: list[str] = []
    geometry_root = ROOT / "src" / "cad_harness" / "geometry"
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name == "subprocess" for alias in node.names):
                    violations.append(f"{path}:{node.lineno}: subprocess import is forbidden")
            elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                violations.append(f"{path}:{node.lineno}: subprocess import is forbidden")
            elif isinstance(node, ast.Call):
                call_name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else ""
                )
                forbidden = call_name in {"SendCommand", "SendStringToExecute"} or (
                    isinstance(node.func, ast.Name) and call_name in {"eval", "exec"}
                )
                if forbidden:
                    violations.append(f"{path}:{node.lineno}: forbidden call {call_name}")
            elif (
                geometry_root in path.parents
                and isinstance(node, ast.Compare)
                and any(isinstance(operator, ast.Eq | ast.NotEq) for operator in node.ops)
                and any(_numeric_expression(item) for item in (node.left, *node.comparators))
            ):
                violations.append(
                    f"{path}:{node.lineno}: numeric equality in geometry must use tolerance"
                )
    return violations


def _walk_features(features: list[object], counts: Counter[str]) -> None:
    for raw in features:
        if not isinstance(raw, dict):
            continue
        feature_type = raw.get("type")
        if isinstance(feature_type, str):
            counts[feature_type] += 1
        children = raw.get("children", [])
        if isinstance(children, list):
            _walk_features(children, counts)


def _golden_violations() -> list[str]:
    import cad_harness.feature_catalog  # noqa: F401 - registration is the API
    from cad_harness.feature_catalog.registry import supported_types

    counts: Counter[str] = Counter()
    for path in sorted((ROOT / "tests" / "golden_drawings").rglob("input_spec.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        features = payload.get("features", [])
        if isinstance(features, list):
            _walk_features(features, counts)
    return [
        f"feature '{feature}' has {counts[feature]} golden cases; at least 3 are required"
        for feature in supported_types()
        if counts[feature] < 3
    ]


def main() -> int:
    violations = [*_source_violations(), *_golden_violations()]
    if violations:
        for violation in violations:
            print(violation)
        return 1
    print("Static invariant check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
