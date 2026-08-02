"""Generate JSON Schemas into contracts/ from the Pydantic models.

Run after any contract change and commit the result. The C# bridge and any non-Python
client validate against these files, so they are part of the public contract.

    uv run python scripts/generate_schemas.py
    uv run python scripts/generate_schemas.py --check    # CI: fail if stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from cad_harness.domain.models.approval import ApprovalRecord
from cad_harness.domain.models.document import DocumentSnapshot, SelectionSnapshot
from cad_harness.domain.models.drawing_spec import DrawingSpec
from cad_harness.domain.models.envelope import ToolResponse
from cad_harness.domain.models.job import CadJob
from cad_harness.domain.models.operation_plan import OperationPlan
from cad_harness.domain.models.result import CommitResult
from cad_harness.domain.models.validation import ValidationReport
from scripts.ipc_envelope_schema import FILENAME as IPC_FILENAME
from scripts.ipc_envelope_schema import IPC_ENVELOPE_SCHEMA

CONTRACTS_DIR = Path(__file__).resolve().parents[1] / "contracts"

#: Output filename -> model. Filenames are part of the contract; do not rename casually.
SCHEMAS: dict[str, type[BaseModel]] = {
    "drawing-spec.schema.json": DrawingSpec,
    "operation-plan.schema.json": OperationPlan,
    "operation-result.schema.json": CommitResult,
    "validation-report.schema.json": ValidationReport,
    "document-snapshot.schema.json": DocumentSnapshot,
    "selection-snapshot.schema.json": SelectionSnapshot,
    "approval-record.schema.json": ApprovalRecord,
    "cad-job.schema.json": CadJob,
    "tool-response.schema.json": ToolResponse,
}


def _dump(schema: dict[str, Any], filename: str) -> str:
    schema = dict(schema)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://cad-harness.local/contracts/{filename}"
    return json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def render(model: type[BaseModel], filename: str) -> str:
    return _dump(model.model_json_schema(), filename)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate JSON Schemas from Pydantic models")
    parser.add_argument(
        "--check", action="store_true", help="Exit non-zero if any file is out of date"
    )
    args = parser.parse_args()

    CONTRACTS_DIR.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []

    rendered: dict[str, str] = {
        filename: render(model, filename) for filename, model in SCHEMAS.items()
    }
    # Hand-written, not model-derived: the bridge IPC envelope.
    rendered[IPC_FILENAME] = _dump(IPC_ENVELOPE_SCHEMA, IPC_FILENAME)

    for filename, content in rendered.items():
        target = CONTRACTS_DIR / filename
        if args.check:
            if not target.is_file() or target.read_text(encoding="utf-8") != content:
                stale.append(filename)
            continue
        target.write_text(content, encoding="utf-8")
        print(f"wrote {target.relative_to(CONTRACTS_DIR.parent)}")

    if stale:
        print("Schemas are out of date:", ", ".join(stale), file=sys.stderr)
        print("Run: uv run python scripts/generate_schemas.py", file=sys.stderr)
        return 1
    if args.check:
        print(f"{len(rendered)} schemas up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
