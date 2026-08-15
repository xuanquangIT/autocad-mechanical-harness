"""Read-only MCP acceptance for a private local DWG/DXF corpus in real AutoCAD.

The runner never opens a customer source file in AutoCAD.  It inventories bounded local
inputs, copies each drawing to an opaque fresh work directory, and opens only that copy
with AutoCAD's read-only flag.  Evidence is an allowlisted aggregate: source names,
paths, drawing text, entity references, and geometry are deliberately excluded.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import sys
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, cast

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from cad_harness.adapters.autocad_com import ComAutoCADAdapter, OwnedComSession
from cad_harness.domain.errors import HarnessError
from cad_harness.security.client_profiles import READ_ONLY_TOOLS
from scripts.live_mcp_r26_acceptance import (
    _SETUP_CONFIRMATIONS,
    TrackedProcess,
    _active_drawing_read_arguments,
    _bridge_path_hash,
    _call,
    _cleanup_acceptance_processes,
    _close_owned_session_preserving_failure,
    _required_acceptance_bundle_root,
    _track_acceptance_processes,
    _trigger_owned_bridge_load,
    _wait_for_owned_com_readiness,
)

_ALLOWED_SUFFIXES = frozenset({".dwg", ".dxf"})
_AUTOCAD_PROG_ID = "AutoCAD.Application.26"
_READ_ONLY_TOOL_SEQUENCE = (
    "cad_status",
    "cad_document_inspect",
    "cad_drawing_read",
    "cad_feature_recognize",
    "cad_audit",
)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_DEFAULT_MAX_CASES = 50
_DEFAULT_MAX_FILE_BYTES = 256 * 1024 * 1024
_DEFAULT_MAX_TOTAL_BYTES = 1024 * 1024 * 1024
_DEFAULT_MAX_ENTITIES = 10_000

if not frozenset(_READ_ONLY_TOOL_SEQUENCE) <= READ_ONLY_TOOLS:  # pragma: no cover
    raise RuntimeError("Corpus acceptance sequence contains a non-read-only MCP tool")


class _ComFactory(Protocol):
    def __call__(
        self, prog_id_key: str = "autocad", *, startup_wait_seconds: float
    ) -> ComAutoCADAdapter: ...


class _CaseWorkflow(Protocol):
    def __call__(
        self,
        *,
        case_id: str,
        expected_display_name: str,
        expected_path_hash: str,
        source_format: str,
        max_entities: int,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class _CorpusCase:
    case_id: str
    source: Path
    scratch: Path
    source_format: str
    size_bytes: int
    source_sha256_before: str
    scratch_sha256_before: str
    source_sha256_after: str | None = None
    scratch_sha256_after: str | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse_point(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _assert_plain_path(path: Path) -> None:
    if path.is_symlink() or _is_reparse_point(path):
        raise ValueError("Corpus input contains a symlink or reparse point")


def _resolve_plain_input_root(input_root: Path) -> Path:
    lexical = Path(os.path.abspath(input_root))
    current = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        current /= part
        if current.exists() or current.is_symlink():
            _assert_plain_path(current)
    return lexical.resolve(strict=True)


def _enumerate_drawings(input_root: Path) -> tuple[Path, ...]:
    root = _resolve_plain_input_root(input_root)
    if not root.is_dir():
        raise NotADirectoryError("Corpus input root must be a directory")
    _assert_plain_path(root)

    drawings: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        _assert_plain_path(directory)
        entries = sorted(directory.iterdir(), key=lambda item: (item.name.casefold(), item.name))
        child_directories: list[Path] = []
        for entry in entries:
            _assert_plain_path(entry)
            if entry.is_dir():
                child_directories.append(entry)
            elif entry.is_file() and entry.suffix.casefold() in _ALLOWED_SUFFIXES:
                drawings.append(entry)
        pending.extend(reversed(child_directories))

    if not drawings:
        raise ValueError("Corpus contains no regular DWG or DXF files")
    return tuple(drawings)


def _validate_positive_limit(name: str, value: int) -> int:
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _roots_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _resolve_roots(
    input_root: Path,
    work_root: Path,
    evidence_root: Path,
) -> tuple[Path, Path, Path]:
    source = _resolve_plain_input_root(input_root)
    work = work_root.resolve()
    evidence = evidence_root.resolve()
    if _roots_overlap(source, work) or _roots_overlap(source, evidence):
        raise ValueError("Work and evidence roots must not overlap the corpus input root")
    return source, work, evidence


def _prepare_cases(
    *,
    input_root: Path,
    run_root: Path,
    max_cases: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> tuple[_CorpusCase, ...]:
    max_cases = _validate_positive_limit("max-cases", max_cases)
    max_file_bytes = _validate_positive_limit("max-file-bytes", max_file_bytes)
    max_total_bytes = _validate_positive_limit("max-total-bytes", max_total_bytes)
    sources = _enumerate_drawings(input_root)
    if len(sources) > max_cases:
        raise ValueError(f"Corpus case limit exceeded ({len(sources)} > {max_cases})")

    inventory: list[tuple[Path, int, str]] = []
    total_bytes = 0
    for source in sources:
        size = source.stat().st_size
        if size <= 0:
            raise ValueError("Corpus contains an empty drawing")
        if size > max_file_bytes:
            raise ValueError(f"Corpus file byte limit exceeded ({size} > {max_file_bytes})")
        total_bytes += size
        if total_bytes > max_total_bytes:
            raise ValueError(
                f"Corpus total byte limit exceeded ({total_bytes} > {max_total_bytes})"
            )
        inventory.append((source, size, _sha256(source)))

    digest_counts = Counter(item[2] for item in inventory)
    digest_ordinals: Counter[str] = Counter()
    prepared: list[_CorpusCase] = []
    for source, size, source_hash in inventory:
        digest_ordinals[source_hash] += 1
        duplicate_suffix = (
            f"-{digest_ordinals[source_hash]:03d}" if digest_counts[source_hash] > 1 else ""
        )
        case_id = f"case-{source_hash}{duplicate_suffix}"
        case_root = run_root / case_id
        case_root.mkdir(parents=False, exist_ok=False)
        source_format = source.suffix.casefold().removeprefix(".")
        scratch = case_root / f"drawing.{source_format}"
        _assert_plain_path(source)
        shutil.copyfile(source, scratch)
        source_after_copy = _sha256(source)
        scratch_hash = _sha256(scratch)
        if source_after_copy != source_hash or scratch_hash != source_hash:
            raise AssertionError(f"Corpus source changed during copy for {case_id}")
        prepared.append(
            _CorpusCase(
                case_id=case_id,
                source=source,
                scratch=scratch,
                source_format=source_format,
                size_bytes=size,
                source_sha256_before=source_hash,
                scratch_sha256_before=scratch_hash,
            )
        )
    return tuple(prepared)


def _finalize_hashes(case: _CorpusCase) -> _CorpusCase:
    source_after = _sha256(case.source)
    scratch_after = _sha256(case.scratch)
    if source_after != case.source_sha256_before:
        raise AssertionError(f"Corpus source was modified for {case.case_id}")
    if scratch_after != case.scratch_sha256_before:
        raise AssertionError(f"Read-only scratch drawing was modified for {case.case_id}")
    return replace(
        case,
        source_sha256_after=source_after,
        scratch_sha256_after=scratch_after,
    )


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AssertionError(f"{label} did not return an object")
    return cast(Mapping[str, Any], value)


def _require_same_revision(
    *,
    label: str,
    payload: Mapping[str, Any],
    document_id: str,
    revision: str,
) -> None:
    if payload.get("document_id") != document_id or payload.get("revision") != revision:
        raise AssertionError(f"{label} provenance did not match the inspected document")


async def _run_read_only_case(
    session: ClientSession,
    *,
    case_id: str,
    expected_display_name: str,
    expected_path_hash: str,
    source_format: str,
    max_entities: int,
) -> dict[str, Any]:
    """Run the exact allowlisted tool sequence and return aggregate-only evidence."""
    tool_statuses: list[dict[str, str]] = []

    status = await _call(session, "cad_status", {})
    tool_statuses.append({"tool": "cad_status", "status": status["status"]})
    status_data = _require_mapping(status.get("data"), "cad_status")
    adapter = _require_mapping(status_data.get("adapter"), "cad_status adapter")
    if adapter.get("adapter_type") != "dotnet_bridge" or adapter.get("available") is not True:
        raise AssertionError("MCP did not connect to an available dotnet_bridge adapter")

    inspected = await _call(session, "cad_document_inspect", {})
    tool_statuses.append({"tool": "cad_document_inspect", "status": inspected["status"]})
    inspected_data = _require_mapping(inspected.get("data"), "cad_document_inspect")
    document_id = inspected_data.get("document_id")
    revision = inspected_data.get("revision")
    if not isinstance(document_id, str) or not isinstance(revision, str):
        raise AssertionError("Inspected document identity was incomplete")
    if (
        adapter.get("active_document_id") != document_id
        or inspected_data.get("display_name") != expected_display_name
        or inspected_data.get("path_hash") != expected_path_hash
    ):
        raise AssertionError("MCP document identity did not match the owned scratch copy")
    if inspected_data.get("read_only") is not True:
        raise AssertionError("Owned scratch document was not opened read-only")

    read_arguments = _active_drawing_read_arguments(document_id)
    read_request = read_arguments["request"]
    read_request["source"]["format"] = source_format
    read_request["max_entities"] = _validate_positive_limit("max-entities", max_entities)
    read = await _call(session, "cad_drawing_read", read_arguments)
    tool_statuses.append({"tool": "cad_drawing_read", "status": read["status"]})
    model = _require_mapping(read.get("data"), "cad_drawing_read")
    _require_same_revision(
        label="Drawing read",
        payload=model,
        document_id=document_id,
        revision=revision,
    )

    recognized = await _call(session, "cad_feature_recognize", {"model": dict(model)})
    tool_statuses.append({"tool": "cad_feature_recognize", "status": recognized["status"]})
    recognition = _require_mapping(recognized.get("data"), "cad_feature_recognize")
    _require_same_revision(
        label="Recognition",
        payload=recognition,
        document_id=document_id,
        revision=revision,
    )

    audited = await _call(session, "cad_audit", {"model": dict(model)})
    tool_statuses.append({"tool": "cad_audit", "status": audited["status"]})
    audit = _require_mapping(audited.get("data"), "cad_audit")
    _require_same_revision(
        label="Audit",
        payload=audit,
        document_id=document_id,
        revision=revision,
    )

    unsupported = model.get("unsupported", ())
    if not isinstance(unsupported, (list, tuple)):
        raise AssertionError("Drawing read returned invalid unsupported-entity evidence")
    unsupported_entity_count = 0
    for entry in unsupported:
        unsupported_entry = _require_mapping(entry, "Unsupported entity")
        count = unsupported_entry.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise AssertionError("Drawing read returned an invalid unsupported count")
        unsupported_entity_count += count

    report = _require_mapping(audit.get("report"), "cad_audit report")
    return {
        "case_id": case_id,
        "tool_statuses": tool_statuses,
        "document": {
            "inspect_revision": revision,
            "read_revision": model.get("revision"),
            "read_only": True,
        },
        "counts": {
            "inspected_entities": int(inspected_data.get("entity_count", 0)),
            "read_entities": len(model.get("entities", ())),
            "layers": len(model.get("layers", ())),
            "dimension_styles": len(model.get("dimension_styles", ())),
            "text_styles": len(model.get("text_styles", ())),
            "recognized_features": len(recognition.get("features", ())),
            "ambiguous_groups": len(recognition.get("ambiguous_groups", ())),
            "open_contours": len(recognition.get("open_contours", ())),
            "audit_findings": len(report.get("findings", ())),
        },
        "coverage": {
            "complete": model.get("coverage_complete") is True,
            "unsupported_type_count": len(unsupported),
            "unsupported_entity_count": unsupported_entity_count,
        },
    }


async def _run_mcp_case_over_stdio_async(
    *,
    case_id: str,
    expected_display_name: str,
    expected_path_hash: str,
    source_format: str,
    max_entities: int,
) -> dict[str, Any]:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "apps.mcp_server"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=dict(os.environ),
        encoding="utf-8",
        encoding_error_handler="strict",
    )
    async with stdio_client(server) as (reader, writer):  # noqa: SIM117
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            return await _run_read_only_case(
                session,
                case_id=case_id,
                expected_display_name=expected_display_name,
                expected_path_hash=expected_path_hash,
                source_format=source_format,
                max_entities=max_entities,
            )


def _run_mcp_case_over_stdio(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(_run_mcp_case_over_stdio_async(**kwargs))


def _close_current_document_no_save(com: ComAutoCADAdapter) -> None:
    document = com._require_document()
    document.Close(False)
    com._document = None


@contextlib.contextmanager
def _temporary_environment(updates: Mapping[str, str | None]) -> Iterator[None]:
    previous = {name: os.environ.get(name) for name in updates}
    try:
        for name, value in updates.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _write_evidence(path: Path, evidence: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(evidence, stream, indent=2, sort_keys=True)
        stream.write("\n")


def _case_evidence(case: _CorpusCase, workflow: Mapping[str, Any]) -> dict[str, Any]:
    if case.source_sha256_after is None or case.scratch_sha256_after is None:
        raise AssertionError("Case hashes were not finalized")
    if workflow.get("case_id") != case.case_id:
        raise AssertionError("Workflow evidence did not match the corpus case")
    document = _require_mapping(workflow.get("document"), "Workflow document evidence")
    counts = _require_mapping(workflow.get("counts"), "Workflow count evidence")
    coverage = _require_mapping(workflow.get("coverage"), "Workflow coverage evidence")
    statuses = workflow.get("tool_statuses")
    if not isinstance(statuses, list):
        raise AssertionError("Workflow tool status evidence was invalid")
    return {
        "case_id": case.case_id,
        "format": case.source_format,
        "bytes": case.size_bytes,
        "tool_statuses": [
            {
                "tool": _require_mapping(status, "Workflow tool status")["tool"],
                "status": _require_mapping(status, "Workflow tool status")["status"],
            }
            for status in statuses
        ],
        "document": {
            "inspect_revision": document["inspect_revision"],
            "read_revision": document["read_revision"],
            "read_only": document["read_only"],
        },
        "counts": {
            name: counts[name]
            for name in (
                "inspected_entities",
                "read_entities",
                "layers",
                "dimension_styles",
                "text_styles",
                "recognized_features",
                "ambiguous_groups",
                "open_contours",
                "audit_findings",
            )
        },
        "coverage": {
            "complete": coverage["complete"],
            "unsupported_type_count": coverage["unsupported_type_count"],
            "unsupported_entity_count": coverage["unsupported_entity_count"],
        },
        "hashes": {
            "source_sha256_before": case.source_sha256_before,
            "source_sha256_after": case.source_sha256_after,
            "scratch_sha256_before": case.scratch_sha256_before,
            "scratch_sha256_after": case.scratch_sha256_after,
        },
    }


def _has_unsupported_case(cases: tuple[dict[str, Any], ...]) -> bool:
    return any(
        case["coverage"]["complete"] is not True
        or case["coverage"]["unsupported_entity_count"] != 0
        for case in cases
    )


def run_acceptance(
    *,
    config_path: Path,
    input_root: Path,
    work_root: Path,
    evidence_root: Path,
    max_cases: int = _DEFAULT_MAX_CASES,
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
    max_entities: int = _DEFAULT_MAX_ENTITIES,
    startup_wait_seconds: float = 120.0,
    bridge_settle_seconds: float = 30.0,
    _com_factory: _ComFactory = ComAutoCADAdapter,
    _case_workflow: _CaseWorkflow = _run_mcp_case_over_stdio,
    _bridge_loader: Callable[[ComAutoCADAdapter], None] = _trigger_owned_bridge_load,
    _sleep: Callable[[float], None] | None = None,
    _run_id_factory: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Run corpus acceptance; all injectable callables are for AutoCAD-free unit tests."""
    import time

    config = config_path.resolve(strict=True)
    source_root, resolved_work_root, resolved_evidence_root = _resolve_roots(
        input_root, work_root, evidence_root
    )
    _validate_positive_limit("max-entities", max_entities)
    if startup_wait_seconds <= 0 or bridge_settle_seconds < 0:
        raise ValueError("AutoCAD wait limits must be non-negative and startup must be positive")
    sleep = _sleep or time.sleep
    run_id = (_run_id_factory or (lambda: f"run-{secrets.token_hex(12)}"))()
    if not run_id.startswith("run-") or not run_id[4:].isalnum():
        raise ValueError("Run id factory returned an unsafe identifier")
    run_root = resolved_work_root / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    cases = _prepare_cases(
        input_root=source_root,
        run_root=run_root,
        max_cases=max_cases,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )

    environment = {
        "CAD_HARNESS_CONFIG": str(config),
        "CAD_HARNESS_ADAPTER": "dotnet_bridge",
        "CAD_HARNESS_MANUAL_GATE_CONFIRMATIONS": _SETUP_CONFIRMATIONS,
        "CAD_HARNESS_SQLITE_PATH": str(run_root / "harness.db"),
        "CAD_HARNESS_PREVIEW_DIR": str(run_root / "previews"),
        "CAD_HARNESS_CHECKPOINT_DIR": str(run_root / "checkpoints"),
        "CAD_HARNESS_CHECKPOINT_ROOT": str(run_root / "bridge-checkpoints"),
        "CAD_HARNESS_COMMIT_JOURNAL_ROOT": str(run_root / "commit-journal"),
        "CAD_HARNESS_ACCEPTANCE_BUNDLE_ROOT": str(_required_acceptance_bundle_root()),
        "CAD_HARNESS_PILOT_RUN_ID": run_id,
        "CAD_HARNESS_LOG_LEVEL": "ERROR",
        "CAD_HARNESS_APPROVAL_SECRET": None,
        "CAD_HARNESS_LIVE_WRITE_VERIFIED": None,
    }

    com = _com_factory("autocad", startup_wait_seconds=startup_wait_seconds)
    acceptance_started_100ns = com._system_filetime_100ns()
    preexisting_pids = com._acad_process_ids()
    tracked_processes: dict[int, TrackedProcess] = {}
    ownership_roots: set[int] = set()
    original_failure: BaseException | None = None
    postexisting_pids: set[int] | None = None
    owned_session: OwnedComSession | None = None
    results: list[dict[str, Any]] = []

    with _temporary_environment(environment):
        try:
            owned_session = com.connect_isolated(versioned_prog_id=_AUTOCAD_PROG_ID)
            if owned_session.pid in preexisting_pids:
                raise AssertionError("Corpus acceptance reused a pre-existing AutoCAD process")
            ownership_roots.add(owned_session.pid)
            _track_acceptance_processes(
                com,
                preexisting_pids=preexisting_pids,
                acceptance_started_100ns=acceptance_started_100ns,
                ownership_roots=ownership_roots,
                tracked=tracked_processes,
            )
            root_record = tracked_processes.get(owned_session.pid)
            if root_record is None or root_record[:2] != (
                owned_session.image_path,
                owned_session.creation_time_100ns,
            ):
                raise AssertionError("Owned AutoCAD process identity was not tracked exactly")
            _wait_for_owned_com_readiness(com, timeout_seconds=startup_wait_seconds)

            bridge_loaded = False
            for case in cases:
                case_failure: BaseException | None = None
                try:
                    com.open_owned_document(case.scratch, read_only=True)
                    if not bridge_loaded:
                        _bridge_loader(com)
                        sleep(bridge_settle_seconds)
                        bridge_loaded = True
                    _track_acceptance_processes(
                        com,
                        preexisting_pids=preexisting_pids,
                        acceptance_started_100ns=acceptance_started_100ns,
                        ownership_roots=ownership_roots,
                        tracked=tracked_processes,
                    )
                    workflow = _case_workflow(
                        case_id=case.case_id,
                        expected_display_name=case.scratch.name,
                        expected_path_hash=_bridge_path_hash(case.scratch),
                        source_format=case.source_format,
                        max_entities=max_entities,
                    )
                except BaseException as exc:
                    case_failure = exc
                    raise
                finally:
                    try:
                        _close_current_document_no_save(com)
                    except BaseException:
                        if case_failure is None:
                            raise
                finalized = _finalize_hashes(case)
                results.append(_case_evidence(finalized, workflow))
        except BaseException as exc:
            original_failure = exc
            raise
        finally:
            cleanup_failure: BaseException | None = None
            try:
                _close_owned_session_preserving_failure(com, original_failure)
            except BaseException as exc:
                cleanup_failure = exc
            try:
                postexisting_pids = _cleanup_acceptance_processes(
                    com,
                    preexisting_pids=preexisting_pids,
                    acceptance_started_100ns=acceptance_started_100ns,
                    ownership_roots=ownership_roots,
                    tracked=tracked_processes,
                )
            except BaseException:
                if original_failure is None and cleanup_failure is None:
                    raise
            if original_failure is None and cleanup_failure is not None:
                raise cleanup_failure

    assert owned_session is not None
    assert postexisting_pids is not None
    case_results = tuple(results)
    unsupported_present = _has_unsupported_case(case_results)
    evidence: dict[str, Any] = {
        "schema_version": "1.0",
        "acceptance_kind": "development_real_autocad_read_only_corpus",
        "accepted": not unsupported_present,
        "run_id": run_id,
        "limits": {
            "max_cases": max_cases,
            "max_file_bytes": max_file_bytes,
            "max_total_bytes": max_total_bytes,
            "max_entities": max_entities,
        },
        "autocad": {
            "versioned_prog_id": _AUTOCAD_PROG_ID,
            "adapter_type": "dotnet_bridge",
            "owned_pid": owned_session.pid,
            "owned_creation_time_100ns": owned_session.creation_time_100ns,
            "tracked_process_count": len(tracked_processes),
            "bridge_load_count": 1,
        },
        "process_baseline": {
            "preexisting_pids": sorted(preexisting_pids),
            "postexisting_pids": sorted(postexisting_pids),
            "unchanged": postexisting_pids == preexisting_pids,
        },
        "case_count": len(case_results),
        "cases": list(case_results),
    }
    if postexisting_pids != preexisting_pids:
        raise AssertionError("Pre-existing AutoCAD process set changed during corpus acceptance")
    evidence_path = resolved_evidence_root / f"{run_id}.json"
    _write_evidence(evidence_path, evidence)
    if unsupported_present:
        raise AssertionError(
            "Corpus acceptance found unsupported entities; redacted evidence was retained"
        )
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--max-cases", type=int, default=_DEFAULT_MAX_CASES)
    parser.add_argument("--max-file-bytes", type=int, default=_DEFAULT_MAX_FILE_BYTES)
    parser.add_argument("--max-total-bytes", type=int, default=_DEFAULT_MAX_TOTAL_BYTES)
    parser.add_argument("--max-entities", type=int, default=_DEFAULT_MAX_ENTITIES)
    parser.add_argument("--startup-wait-seconds", type=float, default=120.0)
    parser.add_argument("--bridge-settle-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        result = run_acceptance(
            config_path=args.config,
            input_root=args.input_root,
            work_root=args.work_root,
            evidence_root=args.evidence_root,
            max_cases=args.max_cases,
            max_file_bytes=args.max_file_bytes,
            max_total_bytes=args.max_total_bytes,
            max_entities=args.max_entities,
            startup_wait_seconds=args.startup_wait_seconds,
            bridge_settle_seconds=args.bridge_settle_seconds,
        )
    except HarnessError as error:
        print(json.dumps({"ok": False, "error": error.to_payload()}, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - live entrypoint
    raise SystemExit(main())
