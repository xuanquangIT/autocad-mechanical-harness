"""Run the real MCP workflow against a Job-owned disposable AutoCAD process."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

from cad_harness.adapters.autocad_com import ComAutoCADAdapter
from cad_harness.application.live_session_proof import LIVE_SESSION_PROOF_ENV
from cad_harness.config import load_settings
from scripts.live_mcp_r26_acceptance import (
    _MANUAL_CONFIRMATIONS_ENV,
    _bridge_path_hash,
    _load_spec,
    _run_mcp_workflow,
    _safe_case_name,
)
from scripts.live_owned_bridge_process import launch_owned_bridge_process

_SCOPED_ENVIRONMENT_KEYS = (
    "CAD_HARNESS_ACCEPTANCE_BUNDLE_ROOT",
    "CAD_HARNESS_CONFIG",
    "CAD_HARNESS_APPROVAL_SECRET",
    _MANUAL_CONFIRMATIONS_ENV,
    LIVE_SESSION_PROOF_ENV,
    "CAD_HARNESS_SQLITE_PATH",
    "CAD_HARNESS_PREVIEW_DIR",
    "CAD_HARNESS_CHECKPOINT_DIR",
    "CAD_HARNESS_LIVE_WRITE_VERIFIED",
    "CAD_HARNESS_LOG_LEVEL",
    "CAD_HARNESS_DURABLE_RESTORE_VERIFIED",
    "CAD_HARNESS_BRIDGE_PIPE_NAME_TEMPLATE",
)


def _scope_acceptance_environment[**P, T](
    function: Callable[P, T],
) -> Callable[P, T]:
    """Restore every runner-owned environment mutation, including on failure."""

    @wraps(function)
    def scoped(*args: P.args, **kwargs: P.kwargs) -> T:
        previous = {name: os.environ.get(name) for name in _SCOPED_ENVIRONMENT_KEYS}
        try:
            return function(*args, **kwargs)
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    return scoped


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _process_snapshot() -> dict[int, tuple[str, int]]:
    return {
        pid: ComAutoCADAdapter._process_identity(pid)
        for pid in ComAutoCADAdapter._acad_process_ids()
    }


def _configure_parent_environment(
    *,
    config_path: Path,
    case_root: Path,
    approval_secret: str,
) -> None:
    os.environ["CAD_HARNESS_CONFIG"] = str(config_path)
    os.environ["CAD_HARNESS_APPROVAL_SECRET"] = approval_secret
    os.environ.pop(_MANUAL_CONFIRMATIONS_ENV, None)
    os.environ.pop(LIVE_SESSION_PROOF_ENV, None)
    os.environ["CAD_HARNESS_SQLITE_PATH"] = str(case_root / "harness.db")
    os.environ["CAD_HARNESS_PREVIEW_DIR"] = str(case_root / "previews")
    os.environ["CAD_HARNESS_CHECKPOINT_DIR"] = str(case_root / "checkpoints")
    os.environ["CAD_HARNESS_LIVE_WRITE_VERIFIED"] = "1"
    os.environ["CAD_HARNESS_LOG_LEVEL"] = "ERROR"
    os.environ.pop("CAD_HARNESS_DURABLE_RESTORE_VERIFIED", None)


@_scope_acceptance_environment
def run_acceptance(
    *,
    config_path: Path,
    spec_path: Path,
    case_name: str,
    drawing_path: Path,
    work_root: Path,
    bundle_root: Path,
    evidence_path: Path,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    case_name = _safe_case_name(case_name)
    config_path = config_path.resolve(strict=True)
    spec = _load_spec(spec_path)
    work_root = work_root.resolve(strict=True)
    drawing_path = drawing_path.resolve(strict=True)
    if work_root not in drawing_path.parents or drawing_path.suffix.casefold() not in {
        ".dwg",
        ".dxf",
    }:
        raise ValueError("Live drawing must be one disposable DWG or DXF under the work root")
    case_root = work_root / case_name
    case_root.mkdir(mode=0o700, exist_ok=False)
    checkpoint_root = case_root / "bridge-checkpoints"
    commit_journal_root = case_root / "bridge-commit-journal"
    checkpoint_root.mkdir()
    commit_journal_root.mkdir()

    bundle_root = bundle_root.resolve(strict=True)
    os.environ["CAD_HARNESS_ACCEPTANCE_BUNDLE_ROOT"] = str(bundle_root)
    approval_secret = secrets.token_urlsafe(48)
    _configure_parent_environment(
        config_path=config_path,
        case_root=case_root,
        approval_secret=approval_secret,
    )
    source_hash_before = _sha256(drawing_path)
    preexisting = _process_snapshot()
    export_root = load_settings(config_path).security.export_path_allowlist[0].resolve()
    export_root.mkdir(parents=True, exist_ok=True)
    export_target = export_root / f"{case_name}-{secrets.token_hex(8)}.dwg"
    if export_target.exists():
        raise FileExistsError("Live acceptance export target already exists")

    workflow: dict[str, Any]
    owned_pid: int
    with launch_owned_bridge_process(
        drawing_path=drawing_path,
        work_root=work_root,
        child_environment_overrides={
            "CAD_HARNESS_APPROVAL_SECRET": approval_secret,
            "CAD_HARNESS_CHECKPOINT_ROOT": str(checkpoint_root),
            "CAD_HARNESS_COMMIT_JOURNAL_ROOT": str(commit_journal_root),
            "CAD_HARNESS_LIVE_WRITE_VERIFIED": "1",
            "CAD_HARNESS_LOG_LEVEL": "ERROR",
        },
    ) as process:
        owned_pid = process.pid
        process.wait_until_ready(timeout_seconds)
        os.environ["CAD_HARNESS_BRIDGE_PIPE_NAME_TEMPLATE"] = process.pipe_template
        workflow = asyncio.run(
            _run_mcp_workflow(
                config_path=config_path,
                spec=spec,
                case_name=case_name,
                expected_display_name=drawing_path.name,
                expected_path_hash=_bridge_path_hash(drawing_path),
                export_target=export_target,
            )
        )

    postexisting = _process_snapshot()
    if postexisting != preexisting:
        raise RuntimeError(
            "AutoCAD process set or identity changed outside the owned acceptance Job"
        )
    if _sha256(drawing_path) != source_hash_before:
        raise RuntimeError("Disposable source drawing bytes changed during in-memory acceptance")
    if not export_target.is_file() or export_target.stat().st_size < 6:
        raise RuntimeError("Live MCP export did not create a DWG artifact")
    with export_target.open("rb") as stream:
        if not stream.read(6).startswith(b"AC"):
            raise RuntimeError("Live MCP export did not create an AutoCAD drawing")

    result: dict[str, Any] = {
        "schema_version": "1.0",
        "real_autocad_evidence": True,
        "production_evidence": False,
        "mcp_transport": "stdio",
        "owned_process": {
            "pid": owned_pid,
            "pipe_pid_matched": True,
            "terminal_after_acceptance": True,
        },
        "preexisting_processes_preserved": True,
        "source_drawing_unchanged": True,
        "export": {
            "sha256": _sha256(export_target),
            "bytes": export_target.stat().st_size,
            "format": "dwg",
        },
        "workflow": workflow,
    }
    evidence_path = evidence_path.resolve()
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--case-name", required=True)
    parser.add_argument("--drawing", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--bundle-root", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    args = parser.parse_args()
    result = run_acceptance(
        config_path=args.config,
        spec_path=args.spec,
        case_name=args.case_name,
        drawing_path=args.drawing,
        work_root=args.work_root,
        bundle_root=args.bundle_root,
        evidence_path=args.evidence,
        timeout_seconds=args.timeout_seconds,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "real_autocad_evidence": result["real_autocad_evidence"],
                "production_evidence": result["production_evidence"],
                "export_sha256": result["export"]["sha256"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
