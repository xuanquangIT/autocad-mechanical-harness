"""Print a deterministic implementation snapshot for autocad-mechanical-harness."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CAPABILITY_PATHS = {
    "drawing_read": "src/cad_harness/application/services/drawing_read_service.py",
    "feature_recognition": "src/cad_harness/comprehension/recognizer.py",
    "takeoff": "src/cad_harness/application/services/takeoff_service.py",
    "drawing_audit": "src/cad_harness/application/services/drawing_audit_service.py",
    "measurement": "src/cad_harness/application/services/measurement_service.py",
    "remediation": "src/cad_harness/application/services/remediation_service.py",
    "engineer_desktop_gate": "apps/engineer_desktop/approval_gate.py",
    "com_reader": "src/cad_harness/adapters/com_drawing_reader.py",
    "bridge_reader": "src/cad_harness/adapters/bridge_drawing_reader.py",
    "bridge_project": "dotnet/AutoCADBridge/CadBridge.Plugin/CadBridge.Plugin.csproj",
    "ci_pipeline": ".github/workflows/ci.yml",
    "raster_intake": "src/cad_harness/comprehension/raster_trace.py",
    "raster_acceptance": "src/cad_harness/application/services/raster_trace_service.py",
    "raster_mcp": "apps/mcp_server/tools/raster_tools.py",
}


def _count_tests(root: Path) -> dict[str, int]:
    tests = root / "tests"
    categories = (
        "unit",
        "property",
        "contract",
        "golden_drawings",
        "fault_injection",
        "integration",
        "performance",
        "compatibility",
    )
    return {
        name: len(tuple((tests / name).rglob("test_*.py"))) if (tests / name).exists() else 0
        for name in categories
    }


def audit(root: Path) -> dict[str, object]:
    tasks_path = root / ".kiro/specs/cad-ai-production-roadmap/tasks.md"
    task_text = tasks_path.read_text(encoding="utf-8")
    completed = len(re.findall(r"^\s*- \[[xX]\]", task_text, flags=re.MULTILINE))
    pending = len(re.findall(r"^\s*- \[ \]", task_text, flags=re.MULTILINE))
    top_level_pending = re.findall(r"^- \[ \] (\d+\.[^\n]+)", task_text, flags=re.MULTILINE)

    return {
        "roadmap": {
            "completed_checkboxes": completed,
            "pending_checkboxes": pending,
            "top_level_pending": top_level_pending,
        },
        "capability_paths": {
            name: {"path": path, "exists": (root / path).exists()}
            for name, path in CAPABILITY_PATHS.items()
        },
        "test_files": _count_tests(root),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    snapshot = audit(args.root.resolve())

    if args.as_json:
        print(json.dumps(snapshot, indent=2, sort_keys=True))
        return 0

    roadmap = snapshot["roadmap"]
    assert isinstance(roadmap, dict)
    print(
        f"Roadmap: {roadmap['completed_checkboxes']} completed, "
        f"{roadmap['pending_checkboxes']} pending"
    )
    print("Pending top-level tasks:")
    for item in roadmap["top_level_pending"]:
        print(f"  - {item}")
    print("Capability paths:")
    capabilities = snapshot["capability_paths"]
    assert isinstance(capabilities, dict)
    for name, state in capabilities.items():
        assert isinstance(state, dict)
        marker = "present" if state["exists"] else "missing"
        print(f"  - {name}: {marker} ({state['path']})")
    print("Test files:")
    tests = snapshot["test_files"]
    assert isinstance(tests, dict)
    for name, count in tests.items():
        print(f"  - {name}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
