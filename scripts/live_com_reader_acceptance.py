"""Opt-in, PID-fenced AutoCAD COM reader acceptance on a copied DXF."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from cad_harness.adapters.autocad_com import ComAutoCADAdapter
from cad_harness.adapters.com_drawing_reader import ComDrawingReader
from cad_harness.domain.errors import (
    AdapterCapabilityMissingError,
    AutoCADBusyError,
)
from cad_harness.domain.models.drawing_model import ReadScope
from cad_harness.domain.ports.drawing_source import DrawingReadRequest, DrawingSourceRef

_STABLE_VARIABLES = (
    "DBMOD",
    "INSUNITS",
    "TILEMODE",
    "CTAB",
    "CVPORT",
    "CLAYER",
    "CELTYPE",
    "CECOLOR",
    "CELWEIGHT",
    "DIMSTYLE",
    "TEXTSTYLE",
    "UCSNAME",
    "PICKFIRST",
    "PICKADD",
    "PICKAUTO",
    "PICKDRAG",
    "PICKSTYLE",
    "SELECTIONPREVIEW",
    "CMDACTIVE",
    "CMDNAMES",
    "OSMODE",
    "ORTHOMODE",
    "SNAPMODE",
    "GRIDMODE",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _plain(value: Any) -> Any:
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    return str(value)


def _state(document: Any, adapter: ComAutoCADAdapter) -> dict[str, Any]:
    model_space = document.ModelSpace
    layers = document.Layers
    selection = document.ActiveSelectionSet
    return {
        "model_space": [
            (str(entity.Handle), adapter._com_entity_type(entity), str(entity.Layer))
            for entity in model_space
        ],
        "layers": [
            (
                str(layer.Name),
                int(layer.Color),
                str(layer.Linetype),
                int(layer.Lineweight),
                bool(layer.Freeze),
                bool(layer.LayerOn),
                bool(layer.Lock),
            )
            for layer in layers
        ],
        "selection_handles": [str(entity.Handle) for entity in selection],
        "variables": {name: _plain(document.GetVariable(name)) for name in _STABLE_VARIABLES},
    }


def _stable_state(document: Any, adapter: ComAutoCADAdapter) -> dict[str, Any]:
    deadline = time.monotonic() + adapter.startup_wait_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            adapter._wait_until_quiescent(timeout_seconds=1.0)
            return _state(document, adapter)
        except AutoCADBusyError as exc:
            last_error = exc
            time.sleep(0.2)
    raise AssertionError("AutoCAD did not expose a stable read-only state") from last_error


def _stable_read(adapter: ComAutoCADAdapter, action: Any) -> Any:
    """Retry only transient COM reads while AutoCAD finishes selection activation."""
    deadline = time.monotonic() + adapter.startup_wait_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            adapter._wait_until_quiescent(timeout_seconds=1.0)
            return action()
        except AutoCADBusyError as exc:
            last_error = exc
            time.sleep(0.2)
    raise AssertionError("AutoCAD did not complete the read-only COM operation") from last_error


def run_acceptance(
    source: Path,
    scratch: Path,
    evidence_path: Path,
    *,
    startup_wait_seconds: float = 180.0,
) -> dict[str, Any]:
    if startup_wait_seconds <= 0:
        raise ValueError("startup_wait_seconds must be positive")
    source = source.resolve(strict=True)
    scratch = scratch.resolve(strict=False)
    evidence_path = evidence_path.resolve(strict=False)
    scratch.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, scratch)
    source_hash = _sha256(scratch)

    adapter = ComAutoCADAdapter("autocad", startup_wait_seconds=startup_wait_seconds)
    preexisting_pids = adapter._acad_process_ids()
    session = adapter.connect_isolated(versioned_prog_id="AutoCAD.Application.26")
    if session.pid in preexisting_pids:
        raise AssertionError("isolated COM session reused a pre-existing AutoCAD PID")

    try:
        document_id = adapter.open_owned_document(scratch, read_only=True)
        document = adapter._require_document()
        if not bool(document.ReadOnly):
            raise AssertionError("scratch acceptance document was not opened read-only")
        selection = document.ActiveSelectionSet
        selection.Clear()
        selection.Select(5)  # acSelectionSetAll; setup occurs before the read-only baseline.
        adapter._wait_until_quiescent(timeout_seconds=adapter.startup_wait_seconds)

        reader = ComDrawingReader(adapter)
        before = _stable_state(document, adapter)
        before_revision = _stable_read(adapter, lambda: reader.current_revision(document_id))
        source_ref = DrawingSourceRef(
            kind="active_document",
            format=scratch.suffix,
            ref=document_id,
        )
        max_entities = max(1, int(document.ModelSpace.Count) + 1)
        summary_request = DrawingReadRequest(
            source=source_ref,
            scope=ReadScope(),
            max_entities=max_entities,
            max_block_nesting_depth=3,
        )
        summary = _stable_read(adapter, lambda: reader.summarize(summary_request))
        refs = tuple(f"acad:handle:{handle}" for handle in before["selection_handles"])
        selection_request = DrawingReadRequest(
            source=source_ref,
            scope=ReadScope(kind="selection", entity_refs=refs),
            max_entities=max_entities,
            max_block_nesting_depth=3,
        )
        selection_summary = _stable_read(adapter, lambda: reader.summarize(selection_request))
        try:
            reader.read(
                DrawingReadRequest(
                    source=source_ref,
                    scope=ReadScope(),
                    max_entities=max_entities,
                    max_block_nesting_depth=3,
                )
            )
        except AdapterCapabilityMissingError as error:
            detailed_read_error = error.details.get("missing_capability")
        else:  # pragma: no cover - live fail-closed assertion
            raise AssertionError("COM reader invented detailed geometry")

        after_revision = _stable_read(adapter, lambda: reader.current_revision(document_id))
        after = _stable_state(document, adapter)
        if before_revision != after_revision or before != after:
            raise AssertionError("COM reader changed drawing, selection, or system-variable state")
        if summary.revision != before_revision or selection_summary.revision != before_revision:
            raise AssertionError("COM summary revision drifted")
        evidence = {
            "schema_version": "1.12",
            "adapter": "com",
            "autocad_pid_owned": True,
            "preexisting_pids_preserved": sorted(preexisting_pids),
            "owned_process": {
                "pid": session.pid,
                "image_name": Path(session.image_path).name,
                "image_path_sha256": _text_sha256(session.image_path.casefold()),
                "creation_time_100ns": session.creation_time_100ns,
            },
            "document_opened_read_only": True,
            "revision_unchanged": True,
            "selection_unchanged": True,
            "system_variables_unchanged": True,
            "model_space_unchanged": True,
            "layer_table_unchanged": True,
            "entity_count": int(document.ModelSpace.Count),
            "selection_count": len(refs),
            "coarse_summary_coverage_complete": summary.coverage_complete,
            "selection_summary_coverage_complete": selection_summary.coverage_complete,
            "detailed_read_error": detailed_read_error,
            "scratch_sha256_before": source_hash,
        }
    finally:
        adapter.close_owned_session()

    deadline = time.monotonic() + 20.0
    while session.pid in adapter._acad_process_ids() and time.monotonic() < deadline:
        time.sleep(0.1)
    if session.pid in adapter._acad_process_ids():
        raise AssertionError("owned AutoCAD process did not terminate")
    postexisting_pids = adapter._acad_process_ids()
    if not preexisting_pids.issubset(postexisting_pids):
        raise AssertionError("a pre-existing AutoCAD process disappeared")
    final_hash = _sha256(scratch)
    if final_hash != source_hash:
        raise AssertionError("read-only COM acceptance changed the scratch file bytes")
    evidence["scratch_sha256_after"] = final_hash
    evidence["scratch_file_unchanged"] = True
    evidence["postexisting_pids"] = sorted(postexisting_pids)
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--startup-wait-seconds", type=float, default=180.0)
    args = parser.parse_args(argv)
    run_acceptance(
        args.source,
        args.scratch,
        args.evidence,
        startup_wait_seconds=args.startup_wait_seconds,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - live entrypoint
    raise SystemExit(main())
